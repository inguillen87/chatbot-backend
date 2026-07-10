import io
import json
from datetime import datetime, timedelta

import jwt
import pytest

from app import db
from models import (
    AnalyticsEventV2,
    ArchivoAdjunto,
    CatalogoItem,
    MarketOrder,
    MunicipioTicket,
    PedidoConversacional,
    PymePedido,
    TenantTicket,
    TenantProfile,
    TicketComentario,
    User,
)


@pytest.fixture(autouse=True)
def _disable_turnstile_enforcement_by_default(monkeypatch, client):
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "false")
    monkeypatch.setitem(client.application.config, "CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "")


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _assert_public_copy_without_internal_jargon(*parts: str) -> None:
    visible_copy = "\n".join(part for part in parts if part)
    for forbidden in ("CRM", "IA", "tenant admin", "auditoria", "operativo", "operativa"):
        assert forbidden not in visible_copy


def test_marketplace_order_note_upload_requires_explicit_tenant(client, init_database, monkeypatch):
    extract_called = {"value": False}

    def fake_extract(*args, **kwargs):
        extract_called["value"] = True
        return []

    monkeypatch.setattr("routes.pedidos_from_file.extract_table_from_file", fake_extract)

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={"pedido_text": "2 chapas galvanizadas"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["codigo"] == "tenant_requerido"
    assert "tenant" in payload["mensaje"].lower()
    assert extract_called["value"] is False


def test_marketplace_order_note_upload_rejects_invalid_turnstile_when_enforced(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-turnstile", nombre="Market Turnstile", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "true")
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-turnstile-secret")
    monkeypatch.setattr("routes.pedidos_from_file.verify_turnstile", lambda *args, **kwargs: False)
    extract_called = {"value": False}

    def fake_extract(*args, **kwargs):
        extract_called["value"] = True
        return []

    monkeypatch.setattr("routes.pedidos_from_file.extract_table_from_file", fake_extract)

    response = client.post(
        "/api/pedidos/from-file",
        data={"pedido_text": "2 chapas galvanizadas"},
        headers={"X-Tenant": tenant.slug, "X-Checkout-Origin": "marketplace"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["codigo"] == "turnstile_verificacion_fallida"
    assert payload["security"]["contract_version"] == "cloudflare.turnstile.public_intake.v1"
    assert payload["security"]["surface"] == "marketplace_assisted_upload"
    assert payload["security"]["status"] == "verification_failed"
    assert payload["security"]["retryable"] is True
    assert payload["security"]["reset_required"] is True
    assert payload["frontend_contract"]["render_as"] == "public_intake_security_error"
    assert payload["frontend_contract"]["can_retry"] is True
    assert payload["frontend_contract"]["reset_turnstile"] is True
    assert extract_called["value"] is False


def test_marketplace_order_note_upload_fails_closed_when_turnstile_enforced_without_secret(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-turnstile-missing", nombre="Market Turnstile Missing", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "true")
    monkeypatch.delenv("CLOUDFLARE_TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.setitem(client.application.config, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "")
    monkeypatch.setitem(client.application.config, "TURNSTILE_SECRET_KEY", "")
    monkeypatch.setattr("routes.pedidos_from_file.verify_turnstile", lambda *args, **kwargs: True)
    extract_called = {"value": False}

    def fake_extract(*args, **kwargs):
        extract_called["value"] = True
        return []

    monkeypatch.setattr("routes.pedidos_from_file.extract_table_from_file", fake_extract)

    response = client.post(
        "/api/pedidos/from-file",
        data={"pedido_text": "2 chapas galvanizadas"},
        headers={"X-Tenant": tenant.slug, "X-Checkout-Origin": "marketplace"},
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["codigo"] == "turnstile_no_configurado"
    assert payload["security"]["contract_version"] == "cloudflare.turnstile.public_intake.v1"
    assert payload["security"]["status"] == "misconfigured"
    assert payload["security"]["configured"] is False
    assert payload["security"]["enforced"] is True
    assert payload["security"]["retryable"] is False
    assert payload["frontend_contract"]["can_retry"] is False
    assert extract_called["value"] is False


def test_marketplace_order_note_upload_creates_assisted_request_contract(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-notes", nombre="Market Notes", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    chapa = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Chapa galvanizada",
        sku="CH-001",
        precio="12000",
        modalidad="venta",
        disponible=True,
    )
    clavos_candidate = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Clavos punta paris",
        sku="CL-002",
        precio="3000",
        unidad="caja",
        modalidad="venta",
        disponible=True,
    )
    db.session.add_all([chapa, clavos_candidate])
    db.session.commit()

    uploaded = {}

    def fake_upload(file_storage, *_, **kwargs):
        assert kwargs.get("kind") == "pedidos"
        uploaded["bytes"] = file_storage.read()
        return {
            "public_url": "https://cdn.example.com/nota.png",
            "original_name": file_storage.filename,
        }

    monkeypatch.setattr("routes.pedidos_from_file.upload_to_gcs", fake_upload)
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {"sku": "CH-001", "nombre": "Chapa galvanizada", "cantidad": 2},
            {"nombre": "Clavos 2 pulgadas", "cantidad": 1},
        ],
    )
    legacy_order_count = PymePedido.query.filter_by(tenant_id=tenant.id).count()

    response = client.post(
        "/api/pedidos/from-file",
        data={"file": (io.BytesIO(b"foto-nota"), "nota.png")},
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug, "X-Checkout-Origin": "marketplace"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert uploaded["bytes"] == b"foto-nota"
    assert payload["contract_version"] == "marketplace.assisted_request.v1"
    assert payload["mode"] == "order_note_upload"
    assert payload["security"]["contract_version"] == "cloudflare.turnstile.public_intake.v1"
    assert payload["security"]["surface"] == "marketplace_assisted_upload"
    assert payload["security"]["status"] == "not_required"
    assert payload["security"]["reset_required"] is False
    assert payload["request_kind"] == "order_note"
    assert payload["request_kind_label"] == "nota de pedido"
    assert payload["source"]["original_filename"] == "nota.png"
    assert payload["source"]["mime_type"] == "image/png"
    assert payload["source"]["file_size_bytes"] == len(b"foto-nota")
    assert payload["attachmentInfo"]["url"] == "https://cdn.example.com/nota.png"
    assert payload["attachmentInfo"]["downloadUrl"] == "https://cdn.example.com/nota.png"
    assert payload["attachmentInfo"]["storage_provider"] == "external"
    assert payload["attachmentInfo"]["storage_access"] == "external"
    assert payload["attachmentInfo"]["is_private"] is False
    assert payload["attachmentInfo"]["name"] == "nota.png"
    assert payload["source"]["attachment_id"] == payload["attachment_id"]
    assert payload["source"]["attachmentInfo"]["url"] == "https://cdn.example.com/nota.png"
    assert payload["source_attachment"]["id"] == payload["attachment_id"]
    assert payload["document_profile"]["primary_intent"] == "create_order_or_quote"
    assert payload["document_profile"]["catalog_matching"] is True
    assert payload["intake_experience"]["contract_version"] == "marketplace.assisted_intake_experience.v1"
    assert payload["intake_experience"]["display_name"] == "Vega Marketplace IA"
    assert payload["intake_experience"]["product_surface"]["name"] == "Vega Marketplace IA"
    assert payload["intake_experience"]["frontend_contract"]["display_name"] == "Vega Marketplace IA"
    assert payload["intake_experience"]["anonymous_intake"] is True
    assert payload["intake_experience"]["catalog_matching"] is True
    assert payload["intake_experience"]["needs_operator_review"] is True
    assert payload["intake_experience"]["frontend_contract"]["render_as"] == "marketplace_assisted_intake"
    assert any(step["id"] == "catalog_match" for step in payload["intake_experience"]["pipeline"])
    _assert_public_copy_without_internal_jargon(
        payload["customer_message"],
        payload["intake_experience"]["title"],
        payload["intake_experience"]["summary"],
        *(f"{step['label']} {step['description']}" for step in payload["intake_experience"]["pipeline"]),
        *(f"{capability['label']} {capability['description']}" for capability in payload["intake_experience"]["capabilities"]),
        payload["intake_experience"]["crm_handoff"]["label"],
        *(f"{action['label']} {action['description']}" for action in payload["next_actions"]),
    )
    assert payload["tenant_slug"] == tenant.slug
    assert payload["pedido_id"] == payload["lead_id"]
    assert payload["match_summary"] == {
        "matched": 1,
        "unmatched": 1,
        "detected": 2,
        "needs_operator_review": True,
    }
    assert payload["items"][0]["catalogo_item_id"] is not None
    assert payload["items_no_encontrados"] == ["1 Clavos 2 pulgadas"]
    assert payload["catalog_candidates"][0]["item"] == "1 Clavos 2 pulgadas"
    candidate = payload["catalog_candidates"][0]["candidates"][0]
    assert candidate["catalogo_item_id"] == clavos_candidate.id
    assert candidate["sku"] == "CL-002"
    assert candidate["score"] > 0
    assert candidate["confidence"] in {"low", "medium", "high"}
    assert candidate["reason"]
    assert payload["crm_state"] == "pending_operator_review"
    assert payload["review_context"]["primary_intent"] == "create_order_or_quote"
    assert "items_sin_match_exacto" in payload["review_context"]["review_reasons"]
    assert payload["review_context"]["operator_queue"] == "commerce_assisted_orders"
    assert payload["review_context"]["priority_reason"] == "contacto_incompleto"
    assert payload["review_context"]["primary_missing_field"] == "contact"
    assert payload["review_context"]["sla_hint"]["minutes"] == 120
    assert payload["operator_pack"]["needs_human_review"] is True
    assert payload["operator_pack"]["operator_queue"] == "commerce_assisted_orders"
    assert payload["operator_pack"]["priority_reason"] == "contacto_incompleto"
    assert payload["operator_pack"]["primary_missing_field"] == "contact"
    assert payload["operator_pack"]["sla_hint"]["minutes"] == 120
    assert "Clavos 2 pulgadas" in payload["operator_pack"]["suggested_reply"]
    assert payload["operator_intake_summary"]["contract_version"] == "marketplace.operator_intake_summary.v1"
    assert payload["operator_intake_summary"]["target_module"] == "orders"
    assert payload["operator_intake_summary"]["operator_queue"] == "commerce_assisted_orders"
    assert payload["operator_intake_summary"]["priority_reason"] == "contacto_incompleto"
    assert payload["operator_intake_summary"]["primary_missing_field"] == "contact"
    assert payload["operator_intake_summary"]["sla_hint"]["minutes"] == 120
    assert payload["operator_intake_summary"]["input"]["file_size_bytes"] == len(b"foto-nota")
    assert payload["operator_intake_summary"]["input"]["mime_type"] == "image/png"
    assert payload["operator_intake_summary"]["input"]["attachment_id"] == payload["attachment_id"]
    assert payload["operator_intake_summary"]["input"]["attachmentInfo"]["url"] == "https://cdn.example.com/nota.png"
    assert payload["operator_intake_summary"]["input"]["attachmentInfo"]["downloadUrl"] == "https://cdn.example.com/nota.png"
    assert payload["operator_intake_summary"]["recommended_next_step"] == "pedir_contacto_y_responder"
    assert payload["operator_intake_summary"]["contact_state"] == "missing"
    assert payload["operator_intake_summary"]["detected_preview"] == ["2 Chapa galvanizada", "1 Clavos 2 pulgadas"]
    assert payload["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert payload["crm_order_draft"]["pedido_id"] == payload["pedido_id"]
    assert payload["crm_order_draft"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert payload["crm_order_draft"]["contact_state"] == "missing"
    assert payload["crm_order_draft"]["recommended_next_step"] == "pedir_contacto_y_responder"
    assert payload["crm_order_draft"]["source_attachment"]["id"] == payload["attachment_id"]
    assert payload["crm_order_draft"]["source"]["attachment_id"] == payload["attachment_id"]
    assert payload["crm_handoff"]["source"]["attachment_id"] == payload["attachment_id"]
    assert payload["crm_handoff"]["source"]["attachmentInfo"]["url"] == "https://cdn.example.com/nota.png"
    assert payload["ticket_type"] == "tenant_ticket"
    assert payload["intake_ticket_id"] == payload["linked_record"]["id"]
    assert payload["linked_record"]["kind"] == "tenant_ticket"
    assert payload["linked_record"]["target_module"] == "orders"
    assert payload["linked_record"]["category"] == "marketplace_assisted_order"
    assert payload["operational_result"]["contract_version"] == "marketplace.assisted_operational_result.v1"
    assert payload["operational_result"]["type"] == "assisted_order_request"
    assert payload["operational_result"]["created_record"] == "tenant_ticket"
    assert payload["operational_result"]["created_record_id"] == payload["intake_ticket_id"]
    assert payload["operational_result"]["status_label"] == "Solicitud recibida"
    assert payload["operational_result"]["requires_operator_confirmation"] is True
    assert payload["operational_result"]["admin_surface"] == "orders"
    assert payload["operational_result"]["tracking_code"] == f"pc-{payload['pedido_id']}"
    assert payload["operational_result"]["tracking_path"] == payload["public_follow_up"]["tracking"]["path"]
    assert payload["crm_handoff"]["materialized_record"]["id"] == payload["intake_ticket_id"]
    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.estado == "nuevo"
    assert pedido.metadata_payload["crm_state"] == "pending_operator_review"
    assert pedido.metadata_payload["linked_record"]["id"] == payload["intake_ticket_id"]
    assert pedido.metadata_payload["linked_record"]["kind"] == "tenant_ticket"
    assert pedido.metadata_payload["operational_result"]["created_record_id"] == payload["intake_ticket_id"]
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == legacy_order_count
    intake_ticket = TenantTicket.query.get(payload["intake_ticket_id"])
    assert intake_ticket is not None
    assert intake_ticket.tenant_id == tenant.id
    assert intake_ticket.categoria == "marketplace_assisted_order"
    assert intake_ticket.estado == "nuevo"
    assert intake_ticket.origen == "marketplace"
    assert intake_ticket.fingerprint.startswith(f"assisted_upload:{tenant.id}:")
    assert intake_ticket.datos_extra["contract_version"] == "marketplace.commerce_intake_ticket.v1"
    assert intake_ticket.datos_extra["pedido_conversacional_id"] == payload["pedido_id"]
    assert intake_ticket.datos_extra["crm_order_draft"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert intake_ticket.datos_extra["operator_pack"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert intake_ticket.datos_extra["source_attachment"]["id"] == payload["attachment_id"]
    assert intake_ticket.datos_extra["attachmentInfo"]["id"] == payload["attachment_id"]
    assert intake_ticket.datos_extra["attachments"][0]["id"] == payload["attachment_id"]
    assert payload["crm_order_draft"]["summary"]["matched"] == 1
    assert payload["crm_order_draft"]["summary"]["unmatched"] == 1
    assert payload["crm_order_draft"]["summary"]["confirmation_status"] == "operator_review_required"
    assert payload["crm_order_draft"]["customer_confirmation"]["contract_version"] == "marketplace.customer_confirmation.v1"
    assert payload["crm_order_draft"]["customer_confirmation"]["status"] == "operator_review_required"
    assert payload["crm_order_draft"]["customer_confirmation"]["confidence_level"] == "medium"
    assert payload["crm_order_draft"]["customer_confirmation"]["primary_action_id"] == "continue_by_whatsapp"
    assert payload["crm_order_draft"]["customer_confirmation"]["blocking_reasons"][0]["id"] == "items_need_review"
    assert [line["status"] for line in payload["crm_order_draft"]["lines"]] == [
        "catalog_matched",
        "needs_catalog_resolution",
    ]
    assert payload["crm_order_draft"]["lines"][0]["catalog_item_id"] == chapa.id
    assert payload["crm_order_draft"]["lines"][0]["confirmation_state"] == "ready"
    assert payload["crm_order_draft"]["lines"][0]["confidence"] == "high"
    assert payload["crm_order_draft"]["lines"][1]["source_name"] == "Clavos 2 pulgadas"
    assert payload["crm_order_draft"]["lines"][1]["candidate_count"] == 1
    assert payload["crm_order_draft"]["lines"][1]["confirmation_state"] == "needs_operator_review"
    assert payload["crm_order_draft"]["lines"][1]["confidence"] == "medium"
    assert payload["crm_handoff"]["draft_order"] == payload["crm_order_draft"]
    assert any(step["id"] == "human_review" for step in payload["customer_next_steps"])
    assert any(action["id"] == "review_unmatched_items" for action in payload["next_actions"])
    tracking_action = next(action for action in payload["next_actions"] if action.get("id") == "tracking")
    assert tracking_action["type"] == "link"
    assert tracking_action["reference"] == f"pedido:{payload['pedido_id']}"
    assert tracking_action["tracking_code"] == f"pc-{payload['pedido_id']}"
    assert tracking_action["href"].startswith(f"/tracking/order/pc-{payload['pedido_id']}?tenant_slug={tenant.slug}&token=")
    whatsapp_action = next(action for action in payload["next_actions"] if action.get("id") == "whatsapp_handoff")
    assert whatsapp_action["type"] == "link"
    assert whatsapp_action["href"].startswith("https://wa.me/?text=")
    assert payload["public_follow_up"]["contract_version"] == "marketplace.assisted_followup.v1"
    assert payload["public_follow_up"]["tracking"]["code"] == f"pc-{payload['pedido_id']}"
    assert payload["public_follow_up"]["tracking"]["token_required"] is True
    assert payload["public_follow_up"]["tracking"]["access"] == "signed_link"
    assert len(payload["public_follow_up"]["tracking"]["token"]) >= 24
    assert f"token={payload['public_follow_up']['tracking']['token']}" in payload["public_follow_up"]["tracking"]["path"]
    assert f"token={payload['public_follow_up']['tracking']['token']}" in payload["public_follow_up"]["tracking"]["api_endpoint"]
    assert payload["public_follow_up"]["tracking"]["path"] == tracking_action["href"]

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido is not None
    assert pedido.tipo == "nota_de_pedido"
    assert pedido.metadata_payload["contract_version"] == "marketplace.assisted_request.v1"
    assert pedido.metadata_payload["request_kind"] == "order_note"
    assert pedido.metadata_payload["document_profile"]["operator_goal"] == "convertir_a_pedido_o_cotizacion"
    assert pedido.metadata_payload["intake_experience"]["render_as"] == "anonymous_assisted_marketplace_intake"
    assert pedido.metadata_payload["source"]["channel"] == "marketplace"
    assert pedido.metadata_payload["source"]["original_filename"] == "nota.png"
    assert pedido.metadata_payload["source"]["file_size_bytes"] == len(b"foto-nota")
    assert pedido.metadata_payload["source"]["attachment_id"] == payload["attachment_id"]
    assert pedido.metadata_payload["source"]["source_attachment"]["url"] == "https://cdn.example.com/nota.png"
    assert pedido.metadata_payload["match_summary"]["unmatched"] == 1
    assert pedido.metadata_payload["operator_pack"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert pedido.metadata_payload["operator_pack"]["priority_reason"] == "contacto_incompleto"
    assert pedido.metadata_payload["operator_intake_summary"]["follow_up"]["code"] == f"pc-{payload['pedido_id']}"
    assert pedido.metadata_payload["operator_intake_summary"]["operator_queue"] == "commerce_assisted_orders"
    assert pedido.metadata_payload["public_follow_up"]["tracking"]["code"] == f"pc-{payload['pedido_id']}"
    assert pedido.metadata_payload["operational_result"]["tracking_code"] == f"pc-{payload['pedido_id']}"
    assert pedido.metadata_payload["crm_order_draft"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert pedido.metadata_payload["crm_order_draft"]["source_attachment"]["id"] == payload["attachment_id"]
    assert pedido.items[0]["attachmentInfo"]["id"] == payload["attachment_id"]
    assert pedido.items[0]["source_attachment"]["url"] == "https://cdn.example.com/nota.png"
    assert pedido.items[0]["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert pedido.items[0]["crm_order_draft"]["source_attachment"]["id"] == payload["attachment_id"]
    assert pedido.items[0]["operational_result"]["created_record"] == "tenant_ticket"
    assert pedido.metadata_payload["catalog_candidates"][0]["candidates"][0]["catalogo_item_id"] == clavos_candidate.id
    assert pedido.items[0]["catalog_candidates"][0]["row"]["nombre"] == "Clavos 2 pulgadas"
    assert pedido.items[0]["public_follow_up"]["tracking"]["path"] == tracking_action["href"]
    analytics_event = AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="assisted_upload_submitted",
        entity_ref=f"pedido:{payload['pedido_id']}",
    ).first()
    assert analytics_event is not None
    assert analytics_event.metadata_payload["contract_version"] == "marketplace.commerce_loop.analytics.v1"
    assert analytics_event.metadata_payload["source"] == "pedidos_from_file"
    assert analytics_event.metadata_payload["matched_count"] == 1
    assert analytics_event.metadata_payload["unmatched_count"] == 1
    assert analytics_event.metadata_payload["needs_operator_review"] is True
    assert analytics_event.metadata_payload["linked_record_type"] == "tenant_ticket"
    assert analytics_event.metadata_payload["linked_record_id"] == payload["intake_ticket_id"]
    assert "contact" not in analytics_event.metadata_payload
    ticket_event = AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="marketplace_intake_ticket_created",
        entity_ref=f"tenant_ticket:{payload['intake_ticket_id']}",
    ).first()
    assert ticket_event is not None
    assert ticket_event.metadata_payload["contract_version"] == "marketplace.commerce_loop.analytics.v1"
    assert ticket_event.metadata_payload["intake_ticket_contract_version"] == "marketplace.commerce_intake_ticket.v1"
    assert ticket_event.metadata_payload["request_kind"] == "order_note"
    assert ticket_event.metadata_payload["target_module"] == "orders"
    assert ticket_event.metadata_payload["ticket_category"] == "marketplace_assisted_order"
    assert ticket_event.metadata_payload["linked_record_type"] == "tenant_ticket"
    assert ticket_event.metadata_payload["linked_record_id"] == payload["intake_ticket_id"]
    assert "contact" not in ticket_event.metadata_payload

    attachment = db.session.get(ArchivoAdjunto, payload["attachment_id"])
    assert attachment is not None
    assert attachment.url == "https://cdn.example.com/nota.png"
    assert attachment.nombre_original == "nota.png"
    assert attachment.mime == "image/png"
    assert attachment.tamano == len(b"foto-nota")
    assert attachment.municipio_ticket_id is None


def test_tenant_crm_rejects_confirm_assisted_order_with_review_gaps(client, app, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-review-gap", nombre="Market Review Gap", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    chapa = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Chapa galvanizada",
        sku="CH-GAP",
        precio="12000",
        modalidad="venta",
        disponible=True,
    )
    clavos_candidate = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Clavos punta paris",
        sku="CL-GAP",
        precio="3000",
        unidad="caja",
        modalidad="venta",
        disponible=True,
    )
    db.session.add_all([chapa, clavos_candidate])
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage, *_, **__: {"public_url": "https://cdn.example.com/nota-gap.png", "original_name": file_storage.filename},
    )
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {"sku": "CH-GAP", "nombre": "Chapa galvanizada", "cantidad": 2},
            {"nombre": "Clavos 2 pulgadas", "cantidad": 1},
        ],
    )

    upload_response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "archivo": (io.BytesIO(b"foto-nota-gap"), "nota-gap.png"),
            "document_type": "order_note",
            "contact_name": "Marcelo",
            "contact_phone": "+5492613168608",
        },
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )
    assert upload_response.status_code == 201
    pedido_id = upload_response.get_json()["pedido_id"]
    crm_id = f"conversational:{pedido_id}"
    headers = _auth_headers(app, owner, tenant.slug)

    detail_response = client.get(f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.get_json()
    assert detail_payload["crm_review_card"]["operational_state"] == "needs_catalog_resolution"
    confirm_action = detail_payload["crm_review_card"]["operator_actions"][0]
    assert confirm_action["id"] == "confirm_order_draft"
    assert confirm_action["enabled"] is False
    assert confirm_action["requires_review"] is True
    assert confirm_action["disabled_reason"] == "resolve_catalog_or_review_pending"

    patch_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"status": "confirmed"},
        headers=headers,
    )
    assert patch_response.status_code == 409
    patch_payload = patch_response.get_json()
    assert patch_payload["error"] == "assisted_order_needs_review"
    blocker_codes = {reason["code"] for reason in patch_payload["blocking_reasons"]}
    assert {"needs_operator_review", "unmatched_items", "catalog_candidates", "customer_confirmation_blocked", "line_needs_review"} & blocker_codes

    db.session.expire_all()
    assert PedidoConversacional.query.get(pedido_id).estado == "nuevo"
    assert PymePedido.query.filter_by(idempotency_key=f"conv_order_{pedido_id}").first() is None

    resolve_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"catalog_resolutions": [{"line_id": "line-2", "catalog_item_id": clavos_candidate.id}]},
        headers=headers,
    )
    assert resolve_response.status_code == 200
    resolved_payload = resolve_response.get_json()
    assert resolved_payload["crm_review_card"]["operational_state"] == "ready_for_order_creation"
    resolved_confirm_action = resolved_payload["crm_review_card"]["operator_actions"][0]
    assert resolved_confirm_action["enabled"] is True
    assert resolved_confirm_action["requires_review"] is False
    assert resolved_payload["assisted_request"]["match_summary"]["matched"] == 2
    assert resolved_payload["assisted_request"]["match_summary"]["unmatched"] == 0
    resolved_line = resolved_payload["assisted_request"]["crm_order_draft"]["lines"][1]
    assert resolved_line["status"] == "catalog_matched"
    assert resolved_line["confirmation_state"] == "ready"
    assert resolved_line["catalog_item_id"] == clavos_candidate.id

    confirm_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"status": "confirmed"},
        headers=headers,
    )
    assert confirm_response.status_code == 200
    assert PymePedido.query.filter_by(idempotency_key=f"conv_order_{pedido_id}").first() is not None


def test_marketplace_order_note_preflight_allows_checkout_origin_header(client, init_database):
    response = client.open(
        "/api/pedidos/from-file?origen=marketplace",
        method="OPTIONS",
        headers={
            "Origin": "http://127.0.0.1:4174",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Checkout-Origin, X-Tenant, X-Turnstile-Token, Idempotency-Key",
        },
    )

    assert response.status_code == 200
    allowed_headers = response.headers.get("Access-Control-Allow-Headers", "")
    assert "X-Checkout-Origin" in allowed_headers
    assert "X-Tenant" in allowed_headers
    assert "X-Turnstile-Token" in allowed_headers
    assert "Idempotency-Key" in allowed_headers


def test_marketplace_order_note_upload_replays_idempotently_without_duplicate_processing(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-idempotent", nombre="Market Idempotent", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    calls = {"upload": 0, "extract": 0}

    def fake_upload(file_storage, *_, **__):
        calls["upload"] += 1
        return {
            "public_url": "https://cdn.example.com/replay.png",
            "original_name": file_storage.filename,
        }

    def fake_extract(content, prompt):
        calls["extract"] += 1
        return [{"nombre": "Chapas galvanizadas", "cantidad": 2}]

    monkeypatch.setattr("routes.pedidos_from_file.upload_to_gcs", fake_upload)
    monkeypatch.setattr("routes.pedidos_from_file.extract_table_from_file", fake_extract)

    headers = {
        "X-Tenant": tenant.slug,
        "X-Checkout-Origin": "marketplace",
        "Idempotency-Key": "market-note-42",
    }
    first = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={"archivo": (io.BytesIO(b"pedido-original"), "pedido.png")},
        content_type="multipart/form-data",
        headers=headers,
    )
    second = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={"archivo": (io.BytesIO(b"pedido-reintento"), "pedido-cambiado.png")},
        content_type="multipart/form-data",
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    first_payload = first.get_json()
    replay_payload = second.get_json()
    assert first_payload["pedido_id"] == replay_payload["pedido_id"]
    assert first_payload["idempotent_replay"] is False
    assert replay_payload["idempotent_replay"] is True
    assert replay_payload["idempotency_key"] == "market-note-42"
    assert calls == {"upload": 1, "extract": 1}
    assert PedidoConversacional.query.filter_by(tenant_id=tenant.id).count() == 1
    assert first_payload["intake_ticket_id"] == replay_payload["intake_ticket_id"]
    assert first_payload["linked_record"]["kind"] == "tenant_ticket"
    assert replay_payload["linked_record"]["kind"] == "tenant_ticket"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 1
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="assisted_upload_submitted",
        entity_ref=f"pedido:{first_payload['pedido_id']}",
    ).count() == 1
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="marketplace_intake_ticket_created",
        entity_ref=f"tenant_ticket:{first_payload['intake_ticket_id']}",
    ).count() == 1

    pedido = PedidoConversacional.query.get(first_payload["pedido_id"])
    assert pedido.metadata_payload["idempotency_key"] == "market-note-42"
    assert pedido.metadata_payload["public_response"]["pedido_id"] == first_payload["pedido_id"]


def test_marketplace_order_note_text_resolves_tenant_from_form(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-form-tenant", nombre="Market Form Tenant", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [{"nombre": "Clavos punta paris", "cantidad": 2}],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "tenant_slug": tenant.slug,
            "pedido_text": "2 cajas de clavos punta paris",
            "contact_name": "Marcelo",
            "contact_phone": "+5492613168608",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["tenant_slug"] == tenant.slug
    assert payload["pedido_id"] == payload["lead_id"]

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido is not None
    assert pedido.tenant_id == tenant.id
    assert pedido.metadata_payload["source"]["channel"] == "marketplace"


def test_marketplace_order_note_accepts_legacy_text_aliases(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-text-alias", nombre="Market Text Alias", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [{"nombre": "Chapas galvanizadas", "cantidad": 4}],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "tenant_slug": tenant.slug,
            "texto_pedido": "4 chapas galvanizadas para cotizar",
            "contact_name": "Marcelo",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["tenant_slug"] == tenant.slug

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido is not None
    assert pedido.items[0]["texto_original"] == "4 chapas galvanizadas para cotizar"


def test_marketplace_order_note_upload_persists_kind_and_contact(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(
        slug="market-contact",
        nombre="Market Contact",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
        dispatch_phone="+54 9 261 000 0000",
    )
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage, *_, **__: {"public_url": "https://cdn.example.com/cotizacion.pdf", "original_name": file_storage.filename},
    )
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [{"producto": "Chapas sinusoidales", "cantidad": "2 unidades"}],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "archivo": (io.BytesIO(b"pedido"), "cotizacion.pdf"),
            "document_type": "quote_request",
            "contact_name": "Marcelo",
            "contact_phone": "+5492613168608",
            "contact_email": "marcelo@example.com",
        },
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "quote_request"
    assert payload["request_kind_label"] == "pedido de cotizacion"
    assert payload["contact"] == {
        "name": "Marcelo",
        "phone": "+5492613168608",
        "email": "marcelo@example.com",
    }
    whatsapp_channel = next(
        channel for channel in payload["public_follow_up"]["channels"] if channel.get("id") == "whatsapp_handoff"
    )
    assert whatsapp_channel["href"].startswith("https://wa.me/5492610000000?text=")
    assert payload["items_no_encontrados"] == ["2 Chapas sinusoidales"]
    assert payload["row_errors"] == []

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.tipo == "nota_de_pedido"
    assert pedido.metadata_payload["contact"]["phone"] == "+5492613168608"
    assert pedido.metadata_payload["source"]["request_kind"] == "quote_request"
    assert pedido.metadata_payload["source"]["channel"] == "marketplace"
    assert pedido.metadata_payload["review_context"]["recommended_channels"] == ["whatsapp", "email", "phone", "crm"]


def test_marketplace_order_note_upload_is_manageable_from_tenant_crm(client, app, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-crm", nombre="Market CRM", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    clavos = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Clavos punta paris",
        sku="CL-01",
        precio="3000",
        modalidad="venta",
        disponible=True,
    )
    db.session.add(clavos)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage, *_, **__: {"public_url": "https://cdn.example.com/pedido.jpg", "original_name": file_storage.filename},
    )
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [{"sku": "CL-01", "producto": "Clavos punta paris", "cantidad": "3 cajas"}],
    )

    upload_response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "archivo": (io.BytesIO(b"nota-ferreteria"), "pedido.jpg"),
            "document_type": "order_note",
            "contact_name": "Marcelo",
            "contact_phone": "+5492613168608",
        },
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )
    assert upload_response.status_code == 201
    pedido_id = upload_response.get_json()["pedido_id"]
    crm_id = f"conversational:{pedido_id}"
    headers = _auth_headers(app, owner, tenant.slug)
    with client.session_transaction() as sess:
        tenant_cart = (sess.get("carritos_pymes") or {}).get(str(tenant.id)) or (sess.get("carritos_pymes") or {}).get(tenant.id)
        assert tenant_cart in (None, [])

    list_response = client.get(f"/api/admin/tenants/{tenant.slug}/orders?limit=25", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.get_json()
    listed = list_payload["orders"]
    listed_order = next(order for order in listed if order["id"] == crm_id)
    order_summary = list_payload["summary"]
    assert list_payload["total"] >= 1
    assert "PedidoConversacional" in list_payload["sources"]
    assert order_summary["contract_version"] == "orders.unified_summary.v1"
    assert order_summary["by_source_model"]["PedidoConversacional"] >= 1
    assert order_summary["by_commercial_stage"][listed_order["commercial_stage"]] >= 1
    assert order_summary["by_channel"][listed_order["channel"]] >= 1
    assert order_summary["assisted_requests"] >= 1
    assert order_summary["ready_for_order_creation"] >= 1
    assert order_summary["crm_focus"]["has_assisted_intake"] is True
    assert listed_order["assisted_request"]["request_kind_label"] == "nota de pedido"
    assert listed_order["assisted_request"]["contact"]["phone"] == "+5492613168608"
    assert listed_order["assisted_request"]["source_attachment"]["url"] == "https://cdn.example.com/pedido.jpg"
    assert listed_order["assisted_request"]["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert listed_order["assisted_request"]["crm_order_draft"]["source_attachment"]["url"] == "https://cdn.example.com/pedido.jpg"
    assert listed_order["assisted_request"]["crm_handoff"]["draft_order"] == listed_order["assisted_request"]["crm_order_draft"]
    assert listed_order["assisted_request"]["crm_handoff"]["source"]["source_attachment"]["url"] == "https://cdn.example.com/pedido.jpg"
    assert listed_order["crm_review_card"]["contract_version"] == "marketplace.crm_review_card.v1"
    assert listed_order["crm_review_card"]["reference"] == f"pedido:{pedido_id}"
    assert listed_order["crm_review_card"]["contact"]["phone"] == "+5492613168608"
    assert listed_order["crm_review_card"]["summary"]["matched"] == 1
    assert listed_order["crm_review_card"]["lines"][0]["catalog_item_id"] == clavos.id
    assert listed_order["crm_review_card"]["suggested_reply"]
    assert listed_order["crm_review_card"]["operational_state"] == "ready_for_order_creation"
    assert listed_order["crm_review_card"]["primary_action_id"] == "confirm_order_draft"
    listed_action = listed_order["crm_review_card"]["operator_actions"][0]
    assert listed_action["id"] == "confirm_order_draft"
    assert listed_action["label"] == "Crear pedido"
    assert listed_action["target_status"] == "confirmed"
    assert listed_action["creates"] == ["pyme_pedido", "market_order"]
    assert listed_action["enabled"] is True
    assert listed_order["customer_profile"]["phone"] == "+5492613168608"

    detail_response = client.get(f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.get_json()
    assert detail_payload["assisted_request"]["match_summary"]["matched"] == 1
    confirmation = detail_payload["assisted_request"]["crm_order_draft"]["customer_confirmation"]
    assert confirmation["status"] == "ready_for_customer_confirmation"
    assert confirmation["confidence_level"] == "high"
    assert confirmation["blocking_reasons"] == []
    assert confirmation["primary_action_id"] == "confirm_order_draft"
    assert detail_payload["assisted_request"]["attachmentInfo"]["url"] == "https://cdn.example.com/pedido.jpg"
    assert detail_payload["assisted_request"]["source"]["attachmentInfo"]["url"] == "https://cdn.example.com/pedido.jpg"
    assert detail_payload["assisted_request"]["public_follow_up"]["tracking"]["code"] == f"pc-{pedido_id}"
    detail_tracking = detail_payload["assisted_request"]["public_follow_up"]["tracking"]
    assert detail_tracking["token_required"] is True
    assert detail_tracking["access"] == "signed_link"
    assert detail_tracking["path"].startswith(f"/tracking/order/pc-{pedido_id}?tenant_slug={tenant.slug}&token=")
    assert f"token={detail_tracking['token']}" in detail_tracking["path"]
    assert detail_payload["assisted_request"]["intake_experience"]["anonymous_intake"] is True
    assert detail_payload["assisted_request"]["intake_experience"]["display_name"] == "Vega Marketplace IA"
    assert detail_payload["assisted_request"]["intake_experience"]["product_surface"]["name"] == "Vega Marketplace IA"
    assert detail_payload["assisted_request"]["intake_experience"]["crm_handoff"]["channels"] == [
        "whatsapp",
        "chat_widget",
        "email",
        "phone",
    ]
    assert detail_payload["assisted_request"]["crm_order_draft"]["reference"] == f"pedido:{pedido_id}"
    assert detail_payload["assisted_request"]["crm_order_draft"]["source_attachment"]["url"] == "https://cdn.example.com/pedido.jpg"
    assert detail_payload["assisted_request"]["crm_handoff"]["draft_order"] == detail_payload["assisted_request"]["crm_order_draft"]
    assert detail_payload["crm_review_card"]["contract_version"] == "marketplace.crm_review_card.v1"
    assert detail_payload["crm_review_card"]["reference"] == f"pedido:{pedido_id}"
    assert detail_payload["crm_review_card"]["status"] == "ready_to_reply"
    assert detail_payload["crm_review_card"]["contact_links"][0]["type"] == "whatsapp"
    assert detail_payload["crm_review_card"]["operational_state"] == "ready_for_order_creation"
    action_ids = [action["id"] for action in detail_payload["crm_review_card"]["operator_actions"]]
    assert action_ids[:2] == ["confirm_order_draft", "reply_customer"]
    assert "open_public_tracking" in action_ids
    confirm_action = detail_payload["crm_review_card"]["operator_actions"][0]
    assert confirm_action["label"] == "Crear pedido"
    assert confirm_action["method"] == "PATCH"
    assert confirm_action["target_status"] == "confirmed"
    assert confirm_action["requires_review"] is False
    reply_action = detail_payload["crm_review_card"]["operator_actions"][1]
    assert reply_action["href"].startswith("https://wa.me/5492613168608?text=")
    assert reply_action["channel"] == "whatsapp"
    assert reply_action["action_label"] == "Responder por WhatsApp"
    operator_pack = detail_payload["assisted_request"]["operator_pack"]
    assert operator_pack["reference"] == f"pedido:{pedido_id}"
    assert operator_pack["priority"] == "normal"
    assert operator_pack["contact_links"][0]["type"] == "whatsapp"
    assert "Detectamos 1 renglon" in operator_pack["suggested_reply"]
    assert any(task["id"] == "confirm_stock_price" for task in operator_pack["suggested_tasks"])

    patch_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"status": "confirmed"},
        headers=headers,
    )
    assert patch_response.status_code == 200
    patch_payload = patch_response.get_json()
    assert patch_payload["status"] == "confirmed"
    assert PedidoConversacional.query.get(pedido_id).estado == "confirmed"

    materialized = PymePedido.query.filter_by(idempotency_key=f"conv_order_{pedido_id}").first()
    assert materialized is not None
    assert materialized.tenant_id == tenant.id
    assert materialized.estado == "confirmado"
    assert materialized.nombre_cliente == "Marcelo"
    assert materialized.telefono_cliente == "+5492613168608"
    detalles = json.loads(materialized.detalles)
    assert detalles[0]["sku"] == "CL-01"
    assert detalles[0]["source"] == "catalog_match"

    market_order = MarketOrder.legacy_safe_query().filter_by(
        tenant_id=tenant.id,
        external_provider="pyme_pedido",
        external_order_id=materialized.nro_pedido,
    ).first()
    assert market_order is not None
    assert market_order.status == "confirmed"
    assert market_order.metadata_payload["source_conversational_id"] == str(pedido_id)
    assert patch_payload["metadata"]["materialized_order"]["nro_pedido"] == materialized.nro_pedido


def test_tenant_crm_confirm_assisted_order_is_atomic_when_materialization_fails(client, app, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-crm-fail", nombre="Market CRM Fail", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Tornillos zincados",
            sku="TOR-01",
            precio="2500",
            modalidad="venta",
            disponible=True,
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage, *_, **__: {"public_url": "https://cdn.example.com/pedido-fallido.jpg", "original_name": file_storage.filename},
    )
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [{"sku": "TOR-01", "producto": "Tornillos zincados", "cantidad": 2}],
    )

    upload_response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "archivo": (io.BytesIO(b"nota-ferreteria"), "pedido-fallido.jpg"),
            "document_type": "order_note",
            "contact_name": "Marcelo",
            "contact_phone": "+5492613168608",
        },
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )
    assert upload_response.status_code == 201
    pedido_id = upload_response.get_json()["pedido_id"]
    crm_id = f"conversational:{pedido_id}"
    headers = _auth_headers(app, owner, tenant.slug)

    monkeypatch.setattr(
        "services.pedido_service.PedidoService.create_from_conversational",
        lambda self, record: None,
    )

    patch_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"status": "confirmed"},
        headers=headers,
    )
    assert patch_response.status_code == 422
    assert patch_response.get_json()["error"] == "materialization_failed"

    db.session.expire_all()
    assert PedidoConversacional.query.get(pedido_id).estado != "confirmed"
    assert PymePedido.query.filter_by(idempotency_key=f"conv_order_{pedido_id}").first() is None


def test_marketplace_text_order_creates_same_assisted_request_contract(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-text", nombre="Market Text", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Clavos punta paris",
            sku="CL-TXT",
            precio="3000",
            modalidad="venta",
            disponible=True,
        )
    )
    db.session.commit()

    upload_called = {"value": False}

    def fake_upload(_file_storage):
        upload_called["value"] = True
        return {"public_url": "https://cdn.example.com/no-deberia-subir.txt"}

    monkeypatch.setattr("routes.pedidos_from_file.upload_to_gcs", fake_upload)
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {"sku": "CL-TXT", "nombre": "Clavos punta paris", "cantidad": 2},
            {"nombre": "Chapas para cotizar", "cantidad": 4},
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "pedido_text": "2 Clavos punta paris\n4 Chapas para cotizar",
            "document_type": "quote_request",
            "contact_name": "Cliente anonimo",
            "contact_phone": "+5492600000000",
        },
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert upload_called["value"] is False
    assert payload["contract_version"] == "marketplace.assisted_request.v1"
    assert payload["request_kind"] == "quote_request"
    assert payload["document_profile"]["input_mode"] == "text"
    assert payload["source"]["input_type"] == "txt"
    assert payload["source"]["archivo_url"] is None
    assert payload["source"]["text_preview"] == "2 Clavos punta paris\n4 Chapas para cotizar"
    assert payload["match_summary"]["matched"] == 1
    assert payload["match_summary"]["unmatched"] == 1
    assert payload["crm_order_draft"]["source"]["input_type"] == "txt"
    assert payload["crm_order_draft"]["source"]["text_preview"] == "2 Clavos punta paris\n4 Chapas para cotizar"
    assert payload["crm_order_draft"]["contact_state"] == "available"
    assert payload["crm_order_draft"]["recommended_next_step"] == "resolver_items_y_cotizar"

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido is not None
    assert pedido.items[0]["texto_original"] == "2 Clavos punta paris\n4 Chapas para cotizar"
    assert pedido.metadata_payload["source"]["text_preview"].startswith("2 Clavos")
    assert pedido.metadata_payload["crm_order_draft"]["reference"] == f"pedido:{payload['pedido_id']}"


def test_marketplace_text_without_document_type_infers_quote_request(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-infer-quote", nombre="Market Infer Quote", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Chapa galvanizada",
            sku="CH-INF",
            precio="12000",
            modalidad="venta",
            disponible=True,
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {"sku": "CH-INF", "nombre": "Chapa galvanizada", "cantidad": 4},
            {"nombre": "Tornillos para presupuesto", "cantidad": 1},
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "pedido_text": "Necesito presupuesto y disponibilidad de 4 chapas galvanizadas y tornillos",
            "contact_name": "Cliente ferreteria",
        },
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "quote_request"
    assert payload["request_kind_label"] == "pedido de cotizacion"
    assert payload["document_profile"]["catalog_matching"] is True
    assert payload["document_profile"]["classification"]["method"] == "heuristic"
    assert "presupuesto" in payload["document_profile"]["classification"]["matched_terms"]
    assert payload["source"]["classification"]["source"] == "text_or_filename"
    assert payload["match_summary"]["matched"] == 1
    assert payload["match_summary"]["unmatched"] == 1


def test_widget_text_order_keeps_session_identity_for_crm(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="widget-order", nombre="Widget Order", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Chapa galvanizada",
            sku="CH-WIDGET",
            precio="12000",
            modalidad="venta",
            disponible=True,
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {"sku": "CH-WIDGET", "nombre": "Chapa galvanizada", "cantidad": 2},
            {"nombre": "Tornillos para cotizar", "cantidad": 1},
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=widget",
        data={
            "pedido_text": "2 chapas galvanizadas\n1 bolsa de tornillos",
            "document_type": "order_note",
            "contact_notes": "Pedido escrito desde el widget publico",
        },
        headers={
            "X-Tenant": tenant.slug,
            "X-Chat-Session-Id": "sid_widget_order_123",
            "X-Anon-Id": "anon-widget-123",
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["source"]["channel"] == "widget"
    assert payload["source"]["chat_session_id"] == "sid_widget_order_123"
    assert payload["source"]["anon_id"] == "anon-widget-123"
    assert payload["match_summary"]["matched"] == 1
    assert payload["match_summary"]["unmatched"] == 1

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.origen == "widget"
    assert pedido.anon_id == "anon-widget-123"
    assert pedido.metadata_payload["source"]["chat_session_id"] == "sid_widget_order_123"
    assert pedido.metadata_payload["source"]["anon_id"] == "anon-widget-123"
    assert pedido.metadata_payload["contact"]["notes"] == "Pedido escrito desde el widget publico"


def test_marketplace_text_without_document_type_infers_tax_bill_contract(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-infer-tax", nombre="Market Infer Tax", tipo="municipio", municipio_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {
                "concepto": "Boleta de tasa municipal cuenta 9988 periodo 06/2026 vencimiento 15/07/2026",
                "cantidad": 1,
            }
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "pedido_text": "Boleta de tasa municipal cuenta 9988 periodo 06/2026 vencimiento 15/07/2026",
            "contact_email": "vecino@example.com",
        },
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "tax_bill"
    assert payload["request_kind_label"] == "boleta de pago o impuesto"
    assert payload["document_profile"]["primary_intent"] == "tax_or_payment_support"
    assert payload["document_profile"]["catalog_matching"] is False
    assert payload["document_profile"]["classification"]["method"] == "heuristic"
    assert "boleta" in payload["document_profile"]["classification"]["matched_terms"]
    assert payload["source"]["classification"]["matched_terms"]
    assert payload["review_context"]["catalog_matching_enabled"] is False
    assert payload["operator_pack"]["contact_links"][0]["type"] == "email"


def test_marketplace_tax_bill_never_matches_catalog_items(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-tax-no-catalog", nombre="Market Tax No Catalog", tipo="municipio", municipio_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.flush()
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Boleta de tasa municipal",
            sku="TASA-001",
            precio="1000",
            disponible=True,
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {
                "concepto": "Boleta de tasa municipal",
                "cuenta": "9988",
                "periodo": "06/2026",
                "vencimiento": "15/07/2026",
                "importe": "$12000",
                "cantidad": 1,
            }
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "pedido_text": "Boleta de tasa municipal cuenta 9988 periodo 06/2026 vencimiento 15/07/2026 importe $12000",
            "contact_email": "vecino@example.com",
        },
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "tax_bill"
    assert payload["items"] == []
    assert payload["catalog_candidates"] == []
    assert payload["match_summary"]["matched"] == 0
    assert payload["match_summary"]["unmatched"] == 1
    assert payload["structured_extraction"]["fields"]["cuenta_o_padron"] == "9988"
    assert payload["structured_extraction"]["fields"]["periodo"] == "06/2026"
    assert payload["structured_extraction"]["fields"]["vencimiento"] == "15/07/2026"
    assert payload["crm_handoff"]["target_module"] == "document_requests"
    assert payload["ticket_type"] == "tenant_ticket"
    assert payload["linked_record"]["kind"] == "tenant_ticket"
    assert payload["linked_record"]["target_module"] == "document_requests"
    assert payload["linked_record"]["category"] == "document_request"
    intake_ticket = TenantTicket.query.get(payload["intake_ticket_id"])
    assert intake_ticket.categoria == "document_request"
    assert intake_ticket.datos_extra["target_module"] == "document_requests"
    assert intake_ticket.datos_extra["request_kind"] == "tax_bill"
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 0
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 0
    assert "stock, precio" not in payload["operator_pack"]["suggested_reply"]
    assert "catalogo" not in payload["operator_pack"]["suggested_reply"].lower()


def test_marketplace_text_without_document_type_infers_municipal_service_request(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-reclamo", nombre="Market Reclamo", tipo="municipio", municipio_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {
                "descripcion": "Luminaria quemada en Don Bosco 55 esquina Sarmiento",
                "cantidad": 1,
            }
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "pedido_text": "Reclamo municipal por luminaria quemada en Don Bosco 55 esquina Sarmiento. De noche queda muy oscuro.",
            "contact_phone": "+5492613168608",
        },
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "service_request"
    assert payload["request_kind_label"] == "reclamo o solicitud vecinal"
    assert payload["document_profile"]["primary_intent"] == "municipal_service_request"
    assert payload["document_profile"]["operator_goal"] == "crear_ticket_o_derivar_area"
    assert payload["document_profile"]["catalog_matching"] is False
    assert payload["review_context"]["primary_intent"] == "municipal_service_request"
    assert payload["operator_pack"]["primary_intent"] == "municipal_service_request"
    assert "area responsable" in payload["operator_pack"]["suggested_reply"]
    assert "stock, precio" not in payload["operator_pack"]["suggested_reply"]
    assert any(task["id"] == "classify_area_and_location" for task in payload["operator_pack"]["suggested_tasks"])
    assert payload["structured_extraction"]["fields"]["categoria_probable"] == "Luminaria"
    assert payload["structured_extraction"]["fields"]["direccion"] == "Don Bosco 55 esquina Sarmiento"
    assert payload["structured_extraction"]["fields"]["descripcion"] == "Luminaria quemada en Don Bosco 55 esquina Sarmiento"
    assert payload["crm_handoff"]["target_module"] == "municipal_claims"
    assert payload["crm_handoff"]["draft_ticket"]["categoria"] == "Luminaria"
    assert payload["crm_handoff"]["draft_ticket"]["direccion"] == "Don Bosco 55 esquina Sarmiento"
    assert payload["crm_handoff"]["draft_ticket"]["estado"] == "nuevo"
    assert payload["crm_handoff"]["recommended_record"] == "municipio_ticket"
    assert payload["linked_record"]["kind"] == "municipio_ticket"
    assert payload["linked_record"]["target_module"] == "municipal_claims"
    assert payload["ticket_id"] == payload["linked_record"]["id"]
    assert payload["ticket_type"] == "municipio"
    assert payload["nro_ticket"].startswith("M-")
    assert payload["consulta_pin"]
    assert payload["public_follow_up"]["tracking"]["kind"] == "claim"
    assert payload["public_follow_up"]["tracking"]["ticket_id"] == payload["ticket_id"]
    assert payload["public_follow_up"]["tracking"]["pin"] == payload["consulta_pin"]
    assert "/tracking/claim/" in payload["public_follow_up"]["tracking"]["path"]

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.tipo == "solicitud_vecinal_desde_archivo"
    assert pedido.metadata_payload["source"]["request_kind"] == "service_request"
    assert pedido.metadata_payload["operator_pack"]["primary_intent"] == "municipal_service_request"
    assert pedido.metadata_payload["crm_handoff"]["target_module"] == "municipal_claims"
    assert pedido.metadata_payload["linked_record"]["id"] == payload["ticket_id"]

    ticket = db.session.get(MunicipioTicket, payload["ticket_id"])
    assert ticket is not None
    assert ticket.tenant_id == tenant.id
    assert ticket.municipio_id == owner.id
    assert ticket.categoria == "Luminaria"
    assert ticket.direccion == "Don Bosco 55 esquina Sarmiento"
    assert ticket.estado == "nuevo"
    assert ticket.telefono_vecino == "+5492613168608"
    assert ticket.nro_ticket == payload["linked_record"]["nro_ticket"]
    assert ticket.consulta_pin == payload["consulta_pin"]
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 1


def test_marketplace_image_without_document_type_reclassifies_after_ocr(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-ocr-reclamo", nombre="Market OCR Reclamo", tipo="municipio", municipio_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage, *_, **__: {"public_url": "https://cdn.example.com/img-1234.jpg", "original_name": file_storage.filename},
    )
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {
                "descripcion": "Luminaria quemada en Don Bosco 55 esquina Sarmiento",
                "direccion": "Don Bosco 55 esquina Sarmiento",
                "cantidad": 1,
            }
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={"archivo": (io.BytesIO(b"foto-reclamo"), "IMG_1234.jpg"), "contact_phone": "+5492613168608"},
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "service_request"
    assert payload["document_profile"]["classification"]["method"] == "post_extraction_heuristic"
    assert payload["document_profile"]["classification"]["previous_kind"] == "order_note"
    assert payload["document_profile"]["classification"]["source"] == "ocr_rows"
    assert payload["document_profile"]["catalog_matching"] is False
    assert payload["items"] == []
    assert payload["catalog_candidates"] == []
    assert payload["crm_handoff"]["target_module"] == "municipal_claims"
    assert payload["crm_handoff"]["draft_ticket"]["categoria"] == "Luminaria"
    assert payload["crm_handoff"]["draft_ticket"]["direccion"] == "Don Bosco 55 esquina Sarmiento"
    assert payload["crm_handoff"]["materialized_record"]["id"] == payload["ticket_id"]
    assert payload["crm_handoff"]["materialized_record"]["display_code"].startswith("M-")
    assert payload["crm_handoff"]["materialized_record"]["attachment_id"] == payload["attachment_id"]
    assert payload["crm_handoff"]["materialized_record"]["source_attachment"]["url"] == "https://cdn.example.com/img-1234.jpg"
    assert payload["public_follow_up"]["tracking"]["kind"] == "claim"
    assert payload["public_follow_up"]["tracking"]["ticket_id"] == payload["ticket_id"]
    assert payload["public_follow_up"]["tracking"]["path"].startswith("/tracking/claim/")
    assert payload["operator_pack"]["primary_intent"] == "municipal_service_request"
    assert payload["attachmentInfo"]["url"] == "https://cdn.example.com/img-1234.jpg"
    assert payload["attachmentInfo"]["downloadUrl"] == "https://cdn.example.com/img-1234.jpg"
    assert payload["attachmentInfo"]["storage_provider"] == "external"
    assert payload["source"]["attachmentInfo"]["url"] == "https://cdn.example.com/img-1234.jpg"
    assert payload["linked_record"]["attachment_id"] == payload["attachment_id"]

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.tipo == "solicitud_vecinal_desde_archivo"
    assert pedido.metadata_payload["source"]["classification"]["method"] == "post_extraction_heuristic"
    assert pedido.metadata_payload["source"]["source_attachment"]["url"] == "https://cdn.example.com/img-1234.jpg"
    assert pedido.metadata_payload["crm_handoff"]["target_module"] == "municipal_claims"
    assert pedido.metadata_payload["linked_record"]["id"] == payload["ticket_id"]
    assert pedido.metadata_payload["linked_record"]["attachment_id"] == payload["attachment_id"]

    ticket = db.session.get(MunicipioTicket, payload["ticket_id"])
    assert ticket is not None
    assert ticket.categoria == "Luminaria"
    assert ticket.direccion == "Don Bosco 55 esquina Sarmiento"
    assert ticket.foto_url_directa == "https://cdn.example.com/img-1234.jpg"
    assert ticket.canal_ingreso == "marketplace_asistido"
    attachment = db.session.get(ArchivoAdjunto, payload["attachment_id"])
    assert attachment is not None
    assert attachment.municipio_ticket_id == ticket.id
    assert attachment.url == "https://cdn.example.com/img-1234.jpg"
    assert attachment.nombre_original == "IMG_1234.jpg"
    comentario = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).one()
    assert comentario.archivo_adjunto_id == attachment.id
    assert comentario.to_dict()["attachmentInfo"]["url"] == "https://cdn.example.com/img-1234.jpg"


def test_marketplace_tax_bill_text_creates_document_review_contract(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-tax", nombre="Market Tax", tipo="municipio", municipio_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        lambda content, prompt: [
            {
                "concepto": "Tasa municipal cuenta 9988 periodo 06/2026 vencimiento 15/07/2026",
                "cantidad": 1,
            }
        ],
    )

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "pedido_text": "Tasa municipal cuenta 9988 periodo 06/2026 vencimiento 15/07/2026",
            "document_type": "tax_bill",
            "contact_name": "Vecino",
            "contact_email": "vecino@example.com",
        },
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "tax_bill"
    assert payload["request_kind_label"] == "boleta de pago o impuesto"
    assert payload["document_profile"]["primary_intent"] == "tax_or_payment_support"
    assert payload["document_profile"]["catalog_matching"] is False
    assert payload["review_context"]["operator_goal"] == "clasificar_documento_y_responder"
    assert payload["intake_experience"]["catalog_matching"] is False
    assert payload["intake_experience"]["crm_handoff"]["recommended_next_action"] == "revisar_y_responder"
    assert payload["operator_pack"]["contact_links"][0]["type"] == "email"
    assert payload["operator_intake_summary"]["target_module"] == "document_requests"
    assert payload["operator_intake_summary"]["recommended_next_step"] == "resolver_faltantes_y_responder"
    assert any(step["id"] == "reply" for step in payload["customer_next_steps"])


def test_marketplace_order_note_upload_creates_review_request_when_extraction_fails(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-review", nombre="Market Review", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage, *_, **__: {"public_url": "https://cdn.example.com/manuscrito.png", "original_name": file_storage.filename},
    )

    def fail_extract(content, prompt):
        raise ValueError("OCR sin confianza")

    monkeypatch.setattr("routes.pedidos_from_file.extract_table_from_file", fail_extract)

    response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={"archivo": (io.BytesIO(b"imagen-dificil"), "manuscrito.png"), "document_type": "other"},
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["request_kind"] == "other"
    assert payload["document_profile"]["primary_intent"] == "manual_review"
    assert payload["review_context"]["primary_intent"] == "manual_review"
    assert "lectura_ia_baja_confianza" in payload["review_context"]["review_reasons"]
    assert payload["match_summary"]["needs_operator_review"] is True
    assert payload["crm_state"] == "pending_operator_review"
    assert payload["items"] == []
    assert "revision del equipo" in payload["customer_message"]
    assert payload["public_follow_up"]["tracking"]["code"] == f"pc-{payload['pedido_id']}"
    assert payload["public_follow_up"]["tracking"]["token_required"] is True
    assert payload["public_follow_up"]["tracking"]["access"] == "signed_link"
    assert "token=" in payload["public_follow_up"]["tracking"]["path"]
    assert any(action["id"] == "whatsapp_handoff" for action in payload["next_actions"])

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.estado == "nuevo"
    assert pedido.metadata_payload["source"]["extraction_error"]


def test_marketplace_order_note_upload_rejects_large_files_before_upload(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-large", nombre="Market Large", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    called = {"upload": False}

    def fake_upload(file_storage):
        called["upload"] = True
        return {"public_url": "https://cdn.example.com/large.pdf"}

    monkeypatch.setattr("routes.pedidos_from_file.upload_to_gcs", fake_upload)

    response = client.post(
        "/api/pedidos/from-file",
        data={"archivo": (io.BytesIO(b"x" * (8 * 1024 * 1024 + 1)), "large.pdf")},
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 413
    assert response.get_json()["codigo"] == "archivo_demasiado_grande"
    assert called["upload"] is False


def test_marketplace_order_note_upload_rejects_unsafe_mime_before_upload(client, init_database, monkeypatch):
    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(slug="market-unsafe", nombre="Market Unsafe", tipo="pyme", pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()

    called = {"upload": False}

    def fake_upload(file_storage):
        called["upload"] = True
        return {"public_url": "https://cdn.example.com/unsafe.png"}

    monkeypatch.setattr("routes.pedidos_from_file.upload_to_gcs", fake_upload)

    response = client.post(
        "/api/pedidos/from-file",
        data={"archivo": (io.BytesIO(b"not-an-image"), "pedido.png", "application/x-msdownload")},
        content_type="multipart/form-data",
        headers={"X-Tenant": tenant.slug},
    )

    assert response.status_code == 415
    assert response.get_json()["codigo"] == "mime_no_permitido"
    assert called["upload"] is False
    assert PedidoConversacional.query.count() == 0
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0
