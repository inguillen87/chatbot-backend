"""Contract tests for the native ``claim_evidence`` WhatsApp Flow.

The implementation is intentionally outside this test module. These tests define
the JSON, tenant-bound lookup, and durable completion contracts that the runtime
must satisfy before the Flow can be published.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import ModuleType
from typing import Any, Callable

import pytest

from app import db
from models import (
    ArchivoAdjunto,
    AuditEvent,
    MessageTemplateRegistry,
    MunicipioTicket,
    ProviderSender,
    TenantProfile,
    TicketComentario,
    User,
    WhatsAppFlowInteraction,
)
from services import meta_flow_json, meta_flow_runtime
from services.meta_flow_data_exchange import MetaFlowActionError, MetaFlowRequestContext
from services.meta_flow_media import DownloadedFlowMedia


FLOW_NAME = "claim_evidence"
LOOKUP_SCREEN = "CLAIM_EVIDENCE_LOOKUP"
PHOTO_SCREEN = "CLAIM_EVIDENCE_PHOTOS"
DOCUMENT_SCREEN = "CLAIM_EVIDENCE_DOCUMENTS"
SUCCESS_SCREEN = "CLAIM_EVIDENCE_SUCCESS"
PHOTO_FIELD = "photos"
DOCUMENT_FIELD = "documents"
TICKET_EVIDENCE_KEY = "whatsapp_flow_evidence"


def _required_callable(module: ModuleType, name: str) -> Callable[..., Any]:
    candidate = getattr(module, name, None)
    assert callable(candidate), f"{module.__name__} must expose callable {name}"
    return candidate


def _required_value(module: ModuleType, name: str) -> Any:
    assert hasattr(module, name), f"{module.__name__} must expose {name}"
    return getattr(module, name)


def _screen(document: dict[str, Any], screen_id: str) -> dict[str, Any]:
    return next(screen for screen in document["screens"] if screen["id"] == screen_id)


def _components(screen: dict[str, Any], component_type: str) -> list[dict[str, Any]]:
    return [
        component
        for component in screen["layout"]["children"]
        if component.get("type") == component_type
    ]


def _footer_action(screen: dict[str, Any]) -> dict[str, Any]:
    footers = _components(screen, "Footer")
    assert len(footers) == 1
    return footers[0]["on-click-action"]


def _tenant_with_sender(
    *,
    slug: str,
    waba_id: str,
    endpoint_alias: str,
) -> tuple[TenantProfile, ProviderSender]:
    owner = User(
        email=f"{slug}@example.test",
        name=slug,
        rol="tenant_admin",
        tipo_chat="municipio",
    )
    owner.set_password("test-pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=slug.title(),
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
        configuracion={
            "meta_platform": {
                "data_exchange": {"endpoint_aliases": [endpoint_alias]}
            }
        },
    )
    db.session.add(tenant)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        channel="whatsapp",
        phone_number=f"+1555{tenant.id:07d}",
        sender_id=f"whatsapp:+1555{tenant.id:07d}",
        waba_id=waba_id,
        phone_number_id=f"phone-{tenant.id}",
        status="active",
        metadata_json={"meta_flow_data_exchange_endpoint_id": endpoint_alias},
    )
    db.session.add(sender)
    db.session.commit()
    return tenant, sender


def _runtime_env(waba_id: str) -> dict[str, str]:
    suffix = "".join(character if character.isalnum() else "_" for character in waba_id)
    prefix = f"META_FLOW_WABA_{suffix.upper()}"
    return {
        f"{prefix}_PRIVATE_KEY_PEM": (
            "-----BEGIN PRIVATE KEY-----\\n"
            "test-private-key-material\\n"
            "-----END PRIVATE KEY-----"
        ),
        f"{prefix}_APP_SECRET": "claim-evidence-app-secret",
        f"{prefix}_FLOW_TOKEN_KEY_V1": (
            "claim-evidence-token-key-with-more-than-thirty-two-bytes"
        ),
    }


def _ticket(
    tenant: TenantProfile,
    *,
    number: str,
    pin: str,
) -> MunicipioTicket:
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        nro_ticket=number,
        consulta_pin=pin,
        pregunta="Bache profundo en la calzada",
        categoria="Via publica",
        estado="en_proceso",
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket


def _interaction(
    *,
    tenant: TenantProfile,
    sender: ProviderSender,
    flow_id: str,
    metadata: dict[str, Any] | None = None,
) -> WhatsAppFlowInteraction:
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"claim-evidence-{tenant.id}",
        language="es",
        status="approved",
        content_sid=f"HXCLAIMEVIDENCE{tenant.id}",
        external_template_id=f"meta-claim-evidence-{tenant.id}",
    )
    db.session.add(registry)
    db.session.flush()
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=flow_id,
        meta_flow_id=registry.external_template_id,
        content_sid=registry.content_sid,
        recipient_hash=hashlib.sha256(f"recipient:{tenant.id}".encode()).hexdigest(),
        recipient_hint="***1234",
        token_digest=hashlib.sha256(f"token:{tenant.id}".encode()).hexdigest(),
        idempotency_key=f"claim-evidence-{tenant.id}",
        status="sent",
        data_contract=["ticket_number", PHOTO_FIELD, DOCUMENT_FIELD],
        metadata_json=metadata or {},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.session.add(interaction)
    db.session.commit()
    return interaction


def _verified_interaction(interaction: WhatsAppFlowInteraction):
    def verify(token: str, **scope: Any) -> dict[str, Any]:
        assert token == "signed-claim-evidence-token"
        assert scope["tenant_id"] == interaction.tenant_id
        assert interaction.flow_id in scope["allowed_flow_ids"]
        assert "secret" in scope
        return {
            "interaction_id": interaction.id,
            "tenant_id": interaction.tenant_id,
            "provider_sender_id": interaction.provider_sender_id,
            "flow_id": interaction.flow_id,
            "meta_flow_id": interaction.meta_flow_id,
            "recipient_hash": interaction.recipient_hash,
            "data_contract": interaction.data_contract,
        }

    return verify


def _context(config: Any, action: str = "data_exchange") -> MetaFlowRequestContext:
    return MetaFlowRequestContext(
        request_id="claim-evidence-contract-test",
        endpoint_id=config.endpoint_id,
        tenant_id=config.tenant_id,
        waba_id=config.waba_id,
        action=action,
    )


def _data_exchange_payload(*, screen: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "3.0",
        "action": "data_exchange",
        "flow_token": "signed-claim-evidence-token",
        "screen": screen,
        "data": data,
    }


def _submission(
    interaction: WhatsAppFlowInteraction,
    answers: dict[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": "whatsapp.flow_submission.v1",
        "flow": {"id": interaction.flow_id, "meta_id": interaction.meta_flow_id},
        "payload": {"answers": answers},
        "correlation": {
            "interaction_id": interaction.id,
            "tenant_id": interaction.tenant_id,
            "provider_sender_id": interaction.provider_sender_id,
        },
    }


def test_claim_evidence_flow_is_publishable_and_separates_media_pickers():
    build_blueprint = _required_callable(
        meta_flow_json,
        "build_claim_evidence_blueprint",
    )
    build_flow = _required_callable(meta_flow_json, "build_claim_evidence_flow")

    blueprint = build_blueprint()
    artifact = build_flow()
    document = artifact.document

    assert isinstance(artifact, meta_flow_json.FlowJsonArtifact)
    assert blueprint.name == FLOW_NAME
    assert blueprint.endpoint_driven is True
    assert artifact.blueprint_name == FLOW_NAME
    assert document["version"] == meta_flow_json.FLOW_JSON_VERSION
    assert document["data_api_version"] == meta_flow_json.DATA_API_VERSION
    assert meta_flow_json.validate_flow_document(document).valid
    assert [screen["id"] for screen in document["screens"]] == [
        LOOKUP_SCREEN,
        PHOTO_SCREEN,
        DOCUMENT_SCREEN,
        SUCCESS_SCREEN,
    ]
    assert document["routing_model"] == {
        LOOKUP_SCREEN: [PHOTO_SCREEN],
        PHOTO_SCREEN: [DOCUMENT_SCREEN],
        DOCUMENT_SCREEN: [SUCCESS_SCREEN],
        SUCCESS_SCREEN: [],
    }

    lookup = _screen(document, LOOKUP_SCREEN)
    assert lookup["sensitive"] == ["access_pin"]
    assert _footer_action(lookup) == {
        "name": "data_exchange",
        "payload": {
            "ticket_number": "${form.ticket_number}",
            "access_pin": "${form.access_pin}",
        },
    }

    photos = _screen(document, PHOTO_SCREEN)
    documents = _screen(document, DOCUMENT_SCREEN)
    photo_picker = _components(photos, "PhotoPicker")
    document_picker = _components(documents, "DocumentPicker")
    assert len(photo_picker) == 1
    assert len(document_picker) == 1
    assert photo_picker[0]["name"] == PHOTO_FIELD
    assert document_picker[0]["name"] == DOCUMENT_FIELD
    assert 1 <= photo_picker[0]["max-uploaded-photos"] <= 5
    assert 1 <= document_picker[0]["max-uploaded-documents"] <= 5
    assert photo_picker[0]["max-file-size-kb"] <= 10 * 1024
    assert document_picker[0]["max-file-size-kb"] <= 10 * 1024
    assert not _components(photos, "DocumentPicker")
    assert not _components(documents, "PhotoPicker")
    assert _footer_action(photos)["name"] == "navigate"
    assert _footer_action(photos)["next"] == {
        "type": "screen",
        "name": DOCUMENT_SCREEN,
    }
    assert _footer_action(documents)["name"] == "navigate"
    assert _footer_action(documents)["next"] == {
        "type": "screen",
        "name": SUCCESS_SCREEN,
    }

    success = _screen(document, SUCCESS_SCREEN)
    assert success["terminal"] is True
    assert success["success"] is True
    assert _components(success, "TextHeading")
    complete = _footer_action(success)
    assert complete == {
        "name": "complete",
        "payload": {
            "ticket_number": (
                "${screen.CLAIM_EVIDENCE_LOOKUP.form.ticket_number}"
            ),
            PHOTO_FIELD: "${screen.CLAIM_EVIDENCE_PHOTOS.form.photos}",
            DOCUMENT_FIELD: (
                "${screen.CLAIM_EVIDENCE_DOCUMENTS.form.documents}"
            ),
        },
    }
    assert "access_pin" not in complete["payload"]


def test_claim_evidence_lookup_is_bound_to_tenant_ticket_and_pin(client):
    flow_id = _required_value(meta_flow_runtime, "CLAIM_EVIDENCE_FLOW_ID")
    assert flow_id == FLOW_NAME

    tenant, sender = _tenant_with_sender(
        slug="claim-evidence-runtime",
        waba_id="waba-claim-evidence",
        endpoint_alias="claim-evidence-endpoint",
    )
    other_tenant, _ = _tenant_with_sender(
        slug="claim-evidence-other",
        waba_id="waba-claim-evidence-other",
        endpoint_alias="claim-evidence-other-endpoint",
    )
    ticket = _ticket(tenant, number="701001", pin="145890")
    other_ticket = _ticket(other_tenant, number="702002", pin="145890")
    interaction = _interaction(tenant=tenant, sender=sender, flow_id=flow_id)
    runtime = meta_flow_runtime.MetaFlowRuntime(
        environ={
            **_runtime_env("waba-claim-evidence"),
            **_runtime_env("waba-claim-evidence-other"),
        },
        token_verifier=_verified_interaction(interaction),
    )
    config = runtime.resolve("claim-evidence-endpoint")
    assert config is not None
    handler = config.handlers["data_exchange"]

    invalid_credentials = [
        {"ticket_number": "M-701001", "access_pin": "999999"},
        {"ticket_number": "M-702002", "access_pin": other_ticket.consulta_pin},
    ]
    for credentials in invalid_credentials:
        with pytest.raises(MetaFlowActionError) as error:
            handler(
                _data_exchange_payload(screen=LOOKUP_SCREEN, data=credentials),
                _context(config),
            )
        assert error.value.code == "claim_not_found"
        assert error.value.status_code == 404

    db.session.refresh(interaction)
    assert "claim_context" not in (interaction.metadata_json or {})

    response = handler(
        _data_exchange_payload(
            screen=LOOKUP_SCREEN,
            data={"ticket_number": "M-701001", "access_pin": ticket.consulta_pin},
        ),
        _context(config),
    )

    db.session.refresh(interaction)
    assert response["screen"] == PHOTO_SCREEN
    assert interaction.metadata_json["claim_context"] == {
        "kind": "municipio",
        "id": str(ticket.id),
        "ticket_number": "M-701001",
    }
    serialized = json.dumps(
        {"response": response, "metadata": interaction.metadata_json},
        sort_keys=True,
    )
    assert ticket.consulta_pin not in serialized
    assert "M-702002" not in serialized


def test_claim_evidence_completion_persists_metadata_once_and_cannot_be_rewritten(
    client,
    monkeypatch,
):
    flow_id = _required_value(meta_flow_runtime, "CLAIM_EVIDENCE_FLOW_ID")
    apply_completion = _required_callable(
        meta_flow_runtime,
        "apply_whatsapp_flow_completion",
    )
    tenant, sender = _tenant_with_sender(
        slug="claim-evidence-completion",
        waba_id="waba-claim-evidence-completion",
        endpoint_alias="claim-evidence-completion-endpoint",
    )
    ticket = _ticket(tenant, number="703003", pin="163470")
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=flow_id,
        metadata={
            "claim_context": {
                "kind": "municipio",
                "id": str(ticket.id),
                "ticket_number": "M-703003",
            }
        },
    )
    photo_content = b"\xff\xd8\xffclaim-evidence-photo"
    document_content = b"%PDF-1.7\nclaim-evidence-document"
    photo_digest = hashlib.sha256(photo_content).hexdigest()
    document_digest = hashlib.sha256(document_content).hexdigest()
    first_answers = {
        "ticket_number": "M-703003",
        PHOTO_FIELD: [
            {
                "id": "photo-media-001",
                "file_name": "bache.jpg",
                "mime_type": "image/jpeg",
                "file_size": len(photo_content),
                "sha256": photo_digest,
                "cdn_url": "https://untrusted.example/photo",
                "ignored": "must-not-be-persisted",
            }
        ],
        DOCUMENT_FIELD: [
            {
                "id": "document-media-001",
                "file_name": "acta.pdf",
                "mime_type": "application/pdf",
                "file_size": len(document_content),
                "sha256": document_digest,
                "cdn_url": "https://untrusted.example/document",
            }
        ],
    }

    def fake_download(**kwargs: Any) -> tuple[DownloadedFlowMedia, ...]:
        assert kwargs["provider_sender"].id == sender.id
        assert [item.media_id for item in kwargs["descriptors"]] == [
            "photo-media-001",
            "document-media-001",
        ]
        return (
            DownloadedFlowMedia(
                kind="photo",
                media_id="photo-media-001",
                file_name="bache.jpg",
                mime_type="image/jpeg",
                content=photo_content,
                sha256_hex=photo_digest,
            ),
            DownloadedFlowMedia(
                kind="document",
                media_id="document-media-001",
                file_name="acta.pdf",
                mime_type="application/pdf",
                content=document_content,
                sha256_hex=document_digest,
            ),
        )

    def fake_store(file_storage: Any, user_id: int | None = None) -> ArchivoAdjunto:
        return ArchivoAdjunto(
            user_id=user_id,
            filename=file_storage.filename,
            nombre_original=file_storage.filename,
            mime=file_storage.content_type,
            tamano=len(file_storage.stream.getvalue()),
            tipo="chat_adjunto",
            url=f"https://cdn.example.test/{file_storage.filename}",
        )

    monkeypatch.setattr(meta_flow_runtime, "download_claim_evidence_media", fake_download)
    monkeypatch.setattr(meta_flow_runtime, "create_attachment_with_thumbnail", fake_store)

    response = apply_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=_submission(interaction, first_answers),
        anon_id="+5491112345678",
    )
    db.session.commit()

    db.session.refresh(ticket)
    db.session.refresh(interaction)
    assert response["fuente"] == "whatsapp_flow_claim_evidence_completed"
    assert response["entity"] == {"kind": "municipio_ticket", "id": ticket.id}
    assert interaction.metadata_json["completion"]["status"] == "applied"
    batches = ticket.datos_extra[TICKET_EVIDENCE_KEY]
    assert len(batches) == 1
    batch = batches[0]
    assert batch["interaction_id"] == interaction.id
    assert batch["source"] == "whatsapp_flow"
    assert batch["ticket_number"] == "M-703003"
    assert len(batch["items"]) == 2
    assert [item["kind"] for item in batch["items"]] == ["photo", "document"]
    assert [item["file_name"] for item in batch["items"]] == ["bache.jpg", "acta.pdf"]
    assert [item["file_size"] for item in batch["items"]] == [
        len(photo_content),
        len(document_content),
    ]
    assert [item["sha256"] for item in batch["items"]] == [
        photo_digest,
        document_digest,
    ]
    assert [item["provider_media_ref"] for item in batch["items"]] == [
        hashlib.sha256(b"photo-media-001").hexdigest(),
        hashlib.sha256(b"document-media-001").hexdigest(),
    ]
    attachment_ids = [item["attachment_id"] for item in batch["items"]]
    assert ArchivoAdjunto.query.filter(ArchivoAdjunto.id.in_(attachment_ids)).count() == 2
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 2
    persisted = json.dumps(batch, sort_keys=True)
    assert "cdn_url" not in persisted
    assert "ignored" not in persisted
    assert "photo-media-001" not in persisted
    assert "document-media-001" not in persisted
    assert ticket.consulta_pin not in persisted
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{flow_id}.completed",
        resource_id=str(ticket.id),
    ).count() == 1
    original_batch = json.loads(json.dumps(batch, sort_keys=True))

    replay_answers = {
        "ticket_number": "M-703003",
        PHOTO_FIELD: [
            {
                "id": "photo-media-001",
                "file_name": "rewritten-name.exe",
                "mime_type": "application/octet-stream",
                "file_size": 1,
            },
            {
                "id": "photo-media-002",
                "file_name": "late-addition.jpg",
                "mime_type": "image/jpeg",
                "file_size": 512,
            },
        ],
        DOCUMENT_FIELD: [],
    }
    replay = apply_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=_submission(interaction, replay_answers),
        anon_id="+5491199999999",
    )
    db.session.commit()

    db.session.refresh(ticket)
    assert replay["fuente"] == "whatsapp_flow_claim_evidence_completed"
    assert ticket.datos_extra[TICKET_EVIDENCE_KEY] == [original_batch]
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{flow_id}.completed",
        resource_id=str(ticket.id),
    ).count() == 1


@pytest.mark.parametrize(
    ("case_name", "answers", "expected_code"),
    [
        (
            "empty",
            {"ticket_number": "M-704004", PHOTO_FIELD: [], DOCUMENT_FIELD: []},
            "claim_evidence_required",
        ),
        (
            "missing_media_id",
            {
                "ticket_number": "M-704004",
                PHOTO_FIELD: [
                    {
                        "file_name": "sin-id.jpg",
                        "mime_type": "image/jpeg",
                        "file_size": 1024,
                    }
                ],
                DOCUMENT_FIELD: [],
            },
            "claim_evidence_metadata_invalid",
        ),
        (
            "wrong_container",
            {
                "ticket_number": "M-704004",
                PHOTO_FIELD: "photo-media-raw-string",
                DOCUMENT_FIELD: [],
            },
            "claim_evidence_metadata_invalid",
        ),
        (
            "oversized_photo",
            {
                "ticket_number": "M-704004",
                PHOTO_FIELD: [
                    {
                        "id": "oversized-photo",
                        "file_name": "oversized.jpg",
                        "mime_type": "image/jpeg",
                        "file_size": (10 * 1024 * 1024) + 1,
                    }
                ],
                DOCUMENT_FIELD: [],
            },
            "claim_evidence_metadata_invalid",
        ),
        (
            "unsafe_document_type",
            {
                "ticket_number": "M-704004",
                PHOTO_FIELD: [],
                DOCUMENT_FIELD: [
                    {
                        "id": "unsafe-document",
                        "file_name": "payload.html",
                        "mime_type": "text/html",
                        "file_size": 2048,
                    }
                ],
            },
            "claim_evidence_metadata_invalid",
        ),
    ],
    ids=[
        "empty",
        "missing_media_id",
        "wrong_container",
        "oversized_photo",
        "unsafe_document_type",
    ],
)
def test_claim_evidence_completion_rejects_empty_or_malformed_metadata(
    client,
    case_name: str,
    answers: dict[str, Any],
    expected_code: str,
):
    flow_id = _required_value(meta_flow_runtime, "CLAIM_EVIDENCE_FLOW_ID")
    apply_completion = _required_callable(
        meta_flow_runtime,
        "apply_whatsapp_flow_completion",
    )
    tenant, sender = _tenant_with_sender(
        slug=f"claim-evidence-invalid-{case_name}",
        waba_id=f"waba-claim-evidence-invalid-{case_name}",
        endpoint_alias=f"claim-evidence-invalid-{case_name}-endpoint",
    )
    ticket = _ticket(tenant, number="704004", pin="174580")
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=flow_id,
        metadata={
            "claim_context": {
                "kind": "municipio",
                "id": str(ticket.id),
                "ticket_number": "M-704004",
            }
        },
    )

    with pytest.raises(MetaFlowActionError) as error:
        apply_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(interaction, answers),
        )

    assert error.value.code == expected_code
    db.session.refresh(ticket)
    assert TICKET_EVIDENCE_KEY not in (ticket.datos_extra or {})
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{flow_id}.completed",
    ).count() == 0
