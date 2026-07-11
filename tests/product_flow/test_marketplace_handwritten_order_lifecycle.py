import io
from datetime import datetime, timedelta, timezone

import jwt

from app import db
from models import (
    CatalogoItem,
    MarketOrder,
    PedidoConversacional,
    PymePedido,
    TenantProfile,
    TenantTicket,
    User,
)


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def test_anonymous_handwritten_marketplace_order_lifecycle(
    client,
    app,
    init_database,
    monkeypatch,
):
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "false")
    monkeypatch.setitem(
        client.application.config,
        "CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE",
        "",
    )
    anon_id = "anon-handwritten-lifecycle"
    client.set_cookie("anon_id", anon_id)

    owner = User.query.filter_by(email="admin@test.com").first()
    tenant = TenantProfile(
        slug="marketplace-handwritten-lifecycle",
        nombre="Ferreteria Lifecycle",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    chapa = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Chapa galvanizada",
        sku="CH-LIFE-01",
        precio="12000",
        modalidad="venta",
        disponible=True,
    )
    clavos = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Clavos punta paris",
        sku="CL-LIFE-02",
        precio="3000",
        unidad="caja",
        modalidad="venta",
        disponible=True,
    )
    db.session.add_all([chapa, clavos])
    db.session.commit()

    photo_bytes = b"foto-lista-manuscrita-lifecycle"
    uploaded = {}
    extracted = {}

    def fake_upload(file_storage, *_, **kwargs):
        uploaded["filename"] = file_storage.filename
        uploaded["bytes"] = file_storage.read()
        uploaded["kind"] = kwargs.get("kind")
        return {
            "public_url": "https://cdn.example.com/lista-manuscrita-lifecycle.jpg",
            "original_name": file_storage.filename,
        }

    def fake_extract(content, prompt):
        extracted["content"] = content
        extracted["prompt"] = prompt
        return [
            {"sku": chapa.sku, "nombre": chapa.nombre, "cantidad": 2},
            {"nombre": "Clavos 2 pulgadas", "cantidad": 1},
        ]

    monkeypatch.setattr("routes.pedidos_from_file.upload_to_gcs", fake_upload)
    monkeypatch.setattr(
        "routes.pedidos_from_file.extract_table_from_file",
        fake_extract,
    )

    intake_response = client.post(
        "/api/pedidos/from-file?origen=marketplace",
        data={
            "archivo": (
                io.BytesIO(photo_bytes),
                "lista-manuscrita-ferreteria.jpg",
            ),
            "contact_name": "Marcelo",
            "contact_phone": "+5492613168608",
        },
        content_type="multipart/form-data",
        headers={
            "X-Tenant": tenant.slug,
            "X-Checkout-Origin": "marketplace",
            "X-Anon-Id": anon_id,
        },
    )

    assert intake_response.status_code == 201, intake_response.get_json()
    intake = intake_response.get_json()
    assert uploaded == {
        "filename": "lista-manuscrita-ferreteria.jpg",
        "bytes": photo_bytes,
        "kind": "pedidos",
    }
    assert extracted["content"] == photo_bytes
    assert "nota manuscrita" in extracted["prompt"].lower()
    assert intake["contract_version"] == "marketplace.assisted_request.v1"
    assert intake["request_kind"] == "handwritten_order"
    assert intake["request_kind_label"] == "nota manuscrita de pedido"
    assert intake["security"]["status"] == "not_required"
    assert intake["intake_experience"]["anonymous_intake"] is True

    pedido_id = intake["pedido_id"]
    ticket_id = intake["intake_ticket_id"]
    crm_id = f"conversational:{pedido_id}"
    tracking = intake["public_follow_up"]["tracking"]
    tracking_code = tracking["code"]
    tracking_token = tracking["token"]
    tracking_path = tracking["path"]
    tracking_api = tracking["api_endpoint"]

    assert tracking_code == f"pc-{pedido_id}"
    assert tracking["access"] == "signed_link"
    assert tracking["token_required"] is True
    assert len(tracking_token) >= 24
    assert f"token={tracking_token}" in tracking_path
    assert f"token={tracking_token}" in tracking_api
    assert intake["crm_handoff"]["materialized_record"]["id"] == ticket_id

    pedido = db.session.get(PedidoConversacional, pedido_id)
    ticket = db.session.get(TenantTicket, ticket_id)
    assert pedido is not None
    assert ticket is not None
    anonymous_user = db.session.get(User, pedido.user_id)
    assert anonymous_user is not None
    assert anonymous_user.id != owner.id
    assert anonymous_user.name == "Visitante"
    assert anonymous_user.anon_id == anon_id
    assert anonymous_user.email == f"anon-{anon_id}@example.invalid"
    assert ticket.user_id == anonymous_user.id
    assert pedido.anon_id == anon_id
    assert pedido.tenant_id == tenant.id
    assert pedido.estado == "nuevo"
    assert pedido.metadata_payload["linked_record"]["id"] == ticket_id
    assert pedido.metadata_payload["public_follow_up"]["tracking"] == tracking
    assert ticket.tenant_id == tenant.id
    assert ticket.categoria == "marketplace_assisted_order"
    assert ticket.datos_extra["pedido_conversacional_id"] == pedido_id
    assert ticket.datos_extra["pedido_reference"] == f"pedido:{pedido_id}"
    assert ticket.datos_extra["public_follow_up"]["tracking"] == tracking

    headers = _auth_headers(app, owner, tenant.slug)
    list_response = client.get(
        f"/api/admin/tenants/{tenant.slug}/orders?limit=25",
        headers=headers,
    )
    assert list_response.status_code == 200, list_response.get_json()
    listed = next(
        order
        for order in list_response.get_json()["orders"]
        if order["id"] == crm_id
    )
    assert listed["source_id"] == pedido_id
    assert listed["source_model"] == "PedidoConversacional"
    assert listed["assisted_request"]["crm_handoff"]["materialized_record"]["id"] == ticket_id
    assert listed["assisted_request"]["public_follow_up"]["tracking"] == tracking
    assert listed["crm_review_card"]["operational_state"] == "needs_catalog_resolution"

    detail_response = client.get(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        headers=headers,
    )
    assert detail_response.status_code == 200, detail_response.get_json()
    detail = detail_response.get_json()
    assert detail["id"] == crm_id
    assert detail["source_id"] == pedido_id
    assert detail["assisted_request"]["crm_handoff"]["materialized_record"]["id"] == ticket_id
    assert detail["assisted_request"]["public_follow_up"]["tracking"] == tracking
    confirm_action = detail["crm_review_card"]["operator_actions"][0]
    assert confirm_action["id"] == "confirm_order_draft"
    assert confirm_action["enabled"] is False
    assert confirm_action["disabled_reason"] == "resolve_catalog_or_review_pending"

    rejected_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"status": "confirmed"},
        headers=headers,
    )
    assert rejected_response.status_code == 409, rejected_response.get_json()
    rejected = rejected_response.get_json()
    assert rejected["error"] == "assisted_order_needs_review"
    assert {reason["code"] for reason in rejected["blocking_reasons"]} & {
        "unmatched_items",
        "catalog_candidates",
        "line_needs_review",
    }
    db.session.expire_all()
    assert db.session.get(PedidoConversacional, pedido_id).estado == "nuevo"
    assert PymePedido.query.filter_by(
        idempotency_key=f"conv_order_{pedido_id}"
    ).first() is None
    assert MarketOrder.legacy_safe_count(tenant_id=tenant.id) == 0

    unresolved_line = next(
        line
        for line in detail["assisted_request"]["crm_order_draft"]["lines"]
        if line["status"] == "needs_catalog_resolution"
    )
    resolve_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={
            "catalog_resolutions": [
                {
                    "line_id": unresolved_line["line_id"],
                    "catalog_item_id": clavos.id,
                }
            ]
        },
        headers=headers,
    )
    assert resolve_response.status_code == 200, resolve_response.get_json()
    resolved = resolve_response.get_json()
    assert resolved["id"] == crm_id
    assert resolved["source_id"] == pedido_id
    assert resolved["crm_review_card"]["operational_state"] == "ready_for_order_creation"
    assert resolved["crm_review_card"]["operator_actions"][0]["enabled"] is True
    assert resolved["assisted_request"]["match_summary"]["matched"] == 2
    assert resolved["assisted_request"]["match_summary"]["unmatched"] == 0
    assert resolved["assisted_request"]["public_follow_up"]["tracking"] == tracking
    resolved_line = next(
        line
        for line in resolved["assisted_request"]["crm_order_draft"]["lines"]
        if line["line_id"] == unresolved_line["line_id"]
    )
    assert resolved_line["status"] == "catalog_matched"
    assert resolved_line["catalog_item_id"] == clavos.id

    confirm_response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}",
        json={"status": "confirmed"},
        headers=headers,
    )
    assert confirm_response.status_code == 200, confirm_response.get_json()
    confirmed = confirm_response.get_json()
    assert confirmed["id"] == crm_id
    assert confirmed["source_id"] == pedido_id
    assert confirmed["status"] == "confirmed"
    assert confirmed["assisted_request"]["public_follow_up"]["tracking"] == tracking

    db.session.expire_all()
    pedido = db.session.get(PedidoConversacional, pedido_id)
    ticket = db.session.get(TenantTicket, ticket_id)
    assert pedido.estado == "confirmed"
    assert pedido.metadata_payload["public_follow_up"]["tracking"] == tracking
    assert ticket.datos_extra["pedido_conversacional_id"] == pedido_id
    assert ticket.datos_extra["public_follow_up"]["tracking"] == tracking

    pyme_pedido = PymePedido.query.filter_by(
        idempotency_key=f"conv_order_{pedido_id}"
    ).one()
    assert pyme_pedido.tenant_id == tenant.id
    assert pyme_pedido.user_id == anonymous_user.id
    assert pyme_pedido.estado == "confirmado"
    assert confirmed["metadata"]["materialized_order"]["id"] == pyme_pedido.id
    assert confirmed["metadata"]["materialized_order"]["nro_pedido"] == pyme_pedido.nro_pedido

    market_order = MarketOrder.legacy_safe_query().filter_by(
        tenant_id=tenant.id,
        external_provider="pyme_pedido",
        external_order_id=pyme_pedido.nro_pedido,
    ).one()
    assert market_order.status == "confirmed"
    assert market_order.user_id == anonymous_user.id
    assert market_order.metadata_payload["pyme_pedido_id"] == pyme_pedido.id
    assert market_order.metadata_payload["source_conversational_id"] == str(pedido_id)

    tracking_response = client.get(
        tracking_api,
        headers={"X-Request-Id": "marketplace-handwritten-lifecycle-tracking"},
    )
    assert tracking_response.status_code == 200, tracking_response.get_json()
    public_tracking = tracking_response.get_json()
    assert public_tracking["contract_version"] == "tracking.experience.v1"
    assert public_tracking["request_id"] == "marketplace-handwritten-lifecycle-tracking"
    assert public_tracking["kind"] == "order"
    assert public_tracking["resource"]["id"] == crm_id
    assert public_tracking["resource"]["code"] == tracking_code
    assert public_tracking["resource"]["source_model"] == "PedidoConversacional"
    assert public_tracking["status"]["raw_status"] == "confirmed"
    assert public_tracking["status"]["current_stage"] == "confirmado"
    assert len(public_tracking["items"]) == 2
    assert {item["status"] for item in public_tracking["items"]} == {"matched_catalog"}
    assert {item["title"] for item in public_tracking["items"]} == {
        "Chapa galvanizada",
        "Clavos 2 pulgadas",
    }
    assert public_tracking["frontend_contract"]["access"] == "signed_link"
    assert public_tracking["actions"][1]["url"] == tracking_path
    assert public_tracking["actions"][1]["requires_token"] is True
