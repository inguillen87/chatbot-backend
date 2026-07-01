import io
import json
from datetime import datetime, timedelta

import jwt

from app import db
from models import CatalogoItem, MarketOrder, MunicipioTicket, PedidoConversacional, PymePedido, TenantProfile, TicketComentario, User


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

    def fake_upload(file_storage):
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
    assert payload["request_kind"] == "order_note"
    assert payload["request_kind_label"] == "nota de pedido"
    assert payload["source"]["original_filename"] == "nota.png"
    assert payload["source"]["mime_type"] == "image/png"
    assert payload["source"]["file_size_bytes"] == len(b"foto-nota")
    assert payload["document_profile"]["primary_intent"] == "create_order_or_quote"
    assert payload["document_profile"]["catalog_matching"] is True
    assert payload["intake_experience"]["contract_version"] == "marketplace.assisted_intake_experience.v1"
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
    assert payload["operator_intake_summary"]["recommended_next_step"] == "pedir_contacto_y_responder"
    assert payload["operator_intake_summary"]["contact_state"] == "missing"
    assert payload["operator_intake_summary"]["detected_preview"] == ["2 Chapa galvanizada", "1 Clavos 2 pulgadas"]
    assert payload["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert payload["crm_order_draft"]["pedido_id"] == payload["pedido_id"]
    assert payload["crm_order_draft"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert payload["crm_order_draft"]["contact_state"] == "missing"
    assert payload["crm_order_draft"]["recommended_next_step"] == "pedir_contacto_y_responder"
    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.estado == "nuevo"
    assert pedido.metadata_payload["crm_state"] == "pending_operator_review"
    assert payload["crm_order_draft"]["summary"]["matched"] == 1
    assert payload["crm_order_draft"]["summary"]["unmatched"] == 1
    assert [line["status"] for line in payload["crm_order_draft"]["lines"]] == [
        "catalog_matched",
        "needs_catalog_resolution",
    ]
    assert payload["crm_order_draft"]["lines"][0]["catalog_item_id"] == chapa.id
    assert payload["crm_order_draft"]["lines"][1]["source_name"] == "Clavos 2 pulgadas"
    assert payload["crm_order_draft"]["lines"][1]["candidate_count"] == 1
    assert payload["crm_handoff"]["draft_order"] == payload["crm_order_draft"]
    assert any(step["id"] == "human_review" for step in payload["customer_next_steps"])
    assert any(action["id"] == "review_unmatched_items" for action in payload["next_actions"])
    tracking_action = next(action for action in payload["next_actions"] if action.get("id") == "tracking")
    assert tracking_action["type"] == "link"
    assert tracking_action["reference"] == f"pedido:{payload['pedido_id']}"
    assert tracking_action["tracking_code"] == f"pc-{payload['pedido_id']}"
    assert tracking_action["href"] == f"/tracking/order/pc-{payload['pedido_id']}?tenant_slug={tenant.slug}"
    whatsapp_action = next(action for action in payload["next_actions"] if action.get("id") == "whatsapp_handoff")
    assert whatsapp_action["type"] == "link"
    assert whatsapp_action["href"].startswith("https://wa.me/?text=")
    assert payload["public_follow_up"]["contract_version"] == "marketplace.assisted_followup.v1"
    assert payload["public_follow_up"]["tracking"]["code"] == f"pc-{payload['pedido_id']}"
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
    assert pedido.metadata_payload["match_summary"]["unmatched"] == 1
    assert pedido.metadata_payload["operator_pack"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert pedido.metadata_payload["operator_pack"]["priority_reason"] == "contacto_incompleto"
    assert pedido.metadata_payload["operator_intake_summary"]["follow_up"]["code"] == f"pc-{payload['pedido_id']}"
    assert pedido.metadata_payload["operator_intake_summary"]["operator_queue"] == "commerce_assisted_orders"
    assert pedido.metadata_payload["public_follow_up"]["tracking"]["code"] == f"pc-{payload['pedido_id']}"
    assert pedido.metadata_payload["crm_order_draft"]["reference"] == f"pedido:{payload['pedido_id']}"
    assert pedido.items[0]["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert pedido.metadata_payload["catalog_candidates"][0]["candidates"][0]["catalogo_item_id"] == clavos_candidate.id
    assert pedido.items[0]["catalog_candidates"][0]["row"]["nombre"] == "Clavos 2 pulgadas"
    assert pedido.items[0]["public_follow_up"]["tracking"]["path"] == tracking_action["href"]


def test_marketplace_order_note_preflight_allows_checkout_origin_header(client, init_database):
    response = client.open(
        "/api/pedidos/from-file?origen=marketplace",
        method="OPTIONS",
        headers={
            "Origin": "http://127.0.0.1:4174",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Checkout-Origin, X-Tenant",
        },
    )

    assert response.status_code == 200
    allowed_headers = response.headers.get("Access-Control-Allow-Headers", "")
    assert "X-Checkout-Origin" in allowed_headers
    assert "X-Tenant" in allowed_headers


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
        lambda file_storage: {"public_url": "https://cdn.example.com/cotizacion.pdf", "original_name": file_storage.filename},
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
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Clavos punta paris",
            sku="CL-01",
            precio="3000",
            modalidad="venta",
            disponible=True,
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "routes.pedidos_from_file.upload_to_gcs",
        lambda file_storage: {"public_url": "https://cdn.example.com/pedido.jpg", "original_name": file_storage.filename},
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

    list_response = client.get(f"/api/admin/tenants/{tenant.slug}/orders?limit=25", headers=headers)
    assert list_response.status_code == 200
    listed = list_response.get_json()["orders"]
    listed_order = next(order for order in listed if order["id"] == crm_id)
    assert listed_order["assisted_request"]["request_kind_label"] == "nota de pedido"
    assert listed_order["assisted_request"]["contact"]["phone"] == "+5492613168608"
    assert listed_order["assisted_request"]["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert listed_order["assisted_request"]["crm_handoff"]["draft_order"] == listed_order["assisted_request"]["crm_order_draft"]
    assert listed_order["customer_profile"]["phone"] == "+5492613168608"

    detail_response = client.get(f"/api/admin/tenants/{tenant.slug}/orders/{crm_id}", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.get_json()
    assert detail_payload["assisted_request"]["match_summary"]["matched"] == 1
    assert detail_payload["assisted_request"]["public_follow_up"]["tracking"]["code"] == f"pc-{pedido_id}"
    assert detail_payload["assisted_request"]["public_follow_up"]["tracking"]["path"] == (
        f"/tracking/order/pc-{pedido_id}?tenant_slug={tenant.slug}"
    )
    assert detail_payload["assisted_request"]["intake_experience"]["anonymous_intake"] is True
    assert detail_payload["assisted_request"]["intake_experience"]["crm_handoff"]["channels"] == [
        "whatsapp",
        "chat_widget",
        "email",
        "phone",
    ]
    assert detail_payload["assisted_request"]["crm_order_draft"]["reference"] == f"pedido:{pedido_id}"
    assert detail_payload["assisted_request"]["crm_handoff"]["draft_order"] == detail_payload["assisted_request"]["crm_order_draft"]
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
    assert market_order.metadata_payload["source_conversational_id"] == str(pedido_id)
    assert patch_payload["metadata"]["materialized_order"]["nro_pedido"] == materialized.nro_pedido


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
        lambda file_storage: {"public_url": "https://cdn.example.com/img-1234.jpg", "original_name": file_storage.filename},
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
    assert payload["public_follow_up"]["tracking"]["kind"] == "claim"
    assert payload["public_follow_up"]["tracking"]["ticket_id"] == payload["ticket_id"]
    assert payload["public_follow_up"]["tracking"]["path"].startswith("/tracking/claim/")
    assert payload["operator_pack"]["primary_intent"] == "municipal_service_request"

    pedido = PedidoConversacional.query.get(payload["pedido_id"])
    assert pedido.tipo == "solicitud_vecinal_desde_archivo"
    assert pedido.metadata_payload["source"]["classification"]["method"] == "post_extraction_heuristic"
    assert pedido.metadata_payload["crm_handoff"]["target_module"] == "municipal_claims"
    assert pedido.metadata_payload["linked_record"]["id"] == payload["ticket_id"]

    ticket = db.session.get(MunicipioTicket, payload["ticket_id"])
    assert ticket is not None
    assert ticket.categoria == "Luminaria"
    assert ticket.direccion == "Don Bosco 55 esquina Sarmiento"
    assert ticket.foto_url_directa == "https://cdn.example.com/img-1234.jpg"
    assert ticket.canal_ingreso == "marketplace_asistido"


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
        lambda file_storage: {"public_url": "https://cdn.example.com/manuscrito.png", "original_name": file_storage.filename},
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
