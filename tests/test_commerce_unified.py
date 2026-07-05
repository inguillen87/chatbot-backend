import json

from app import create_app, db
from config import TestConfig
from models import MarketOrder, MarketOrderItem, PedidoConversacional, PymePedido, TenantProfile, User
from services.commerce_contracts import build_customer_profile
from services.commerce_unified import dedupe_unified_orders, serialize_unified_order


def test_build_customer_profile_normalizes_phone_channels_and_identity():
    user = type(
        "UserStub",
        (),
        {"id": 77, "name": "  Ana Buyer ", "email": "ANA@Buyer.com ", "telefono": None, "anon_id": "whatsapp:+5491166677788"},
    )()

    profile = build_customer_profile(user=user, payload={"channel": "telefono"}, session_id="session-77")

    assert profile["channel"] == "phone"
    assert profile["channel_group"] == "conversational"
    assert profile["phone"] == "+5491166677788"
    assert profile["contact_key"] == "user:77"
    assert profile["identity"]["display_name"] == "Ana Buyer"
    assert profile["avatar_url"] is None
    assert profile["avatar_consent"] is False
    assert profile["avatar_policy"] == "consented_upload_or_social_only"


def test_serialize_unified_order_supports_multiple_models():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        owner = User(email="owner-unified@test.com", name="Owner Unified", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        owner.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/owner.webp",
                "avatar_source": "profile_upload",
                "avatar_consent": True,
            }
        }
        db.session.add(owner)
        db.session.commit()

        tenant = TenantProfile(slug="tenant-unified", nombre="Tenant Unified", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        market = MarketOrder(
            tenant_id=tenant.id,
            user_id=owner.id,
            status="pending",
            contact_name="Buyer",
            contact_phone="+5491122233344",
            contact_email="buyer@test.com",
            contact_key="user:1",
            channel="voice",
            total_monetary=5500,
            currency="ARS",
        )
        market.items.append(MarketOrderItem(quantity=2, name_snapshot="Yerba", price_monetary=2750, currency="ARS"))
        db.session.add(market)

        conv = PedidoConversacional(
            tenant_id=tenant.id,
            user_id=owner.id,
            estado="pendiente_pago",
            monto_monetario=1200,
            origen="whatsapp",
            items=[{"title": "Café", "quantity": 1, "unit_price": 1200, "currency_id": "ARS"}],
            metadata_payload={"contact_key": "email:test@test.com", "contacto": {"nombre": "Ana", "email": "test@test.com"}},
        )
        db.session.add(conv)

        legacy = PymePedido(
            pyme_id=owner.id,
            tenant_id=tenant.id,
            asunto="Pedido",
            detalles=json.dumps([{"nombre": "Pan", "cantidad": 3, "precio_unitario": 1000}], ensure_ascii=False),
            monto_total=3000,
            nombre_cliente="Buyer Legacy",
            email_cliente="legacy@test.com",
            telefono_cliente="+5491100000000",
            user_id=owner.id,
        )
        db.session.add(legacy)
        db.session.commit()

        serialized_market = serialize_unified_order(market)
        serialized_conv = serialize_unified_order(conv)
        serialized_legacy = serialize_unified_order(legacy)

        assert serialized_market["channel"] == "phone"
        assert serialized_market["commercial_stage"] == "awaiting_confirmation"
        assert serialized_market["items"][0]["title"] == "Yerba"
        assert serialized_conv["commercial_stage"] == "awaiting_payment"
        assert serialized_conv["contact"]["contact_key"] == "email:test@test.com"
        assert serialized_legacy["source_model"] == "PymePedido"
        assert serialized_legacy["items"][0]["title"] == "Pan"
        assert serialized_market["customer_profile"]["avatar_url"] == "https://cdn.example.com/profile/owner.webp"
        assert serialized_market["customer_profile"]["identity"]["avatar_source"] == "profile_upload"
        assert serialized_conv["customer_profile"]["avatar_consent"] is True
        assert serialized_legacy["customer_identity"]["avatar_policy"] == "consented_upload_or_social_only"
        assert serialized_legacy["contact"]["avatar_url"] == "https://cdn.example.com/profile/owner.webp"


def test_serialize_unified_order_hides_unconsented_profile_avatar():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        customer = User(email="unconsented-avatar@test.com", name="No Avatar", rol="user", tipo_chat="pyme")
        customer.set_password("pass")
        customer.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/unconsented.webp",
                "avatar_source": "profile_upload",
                "avatar_consent": False,
            }
        }
        db.session.add(customer)
        db.session.commit()

        order = MarketOrder(
            tenant_id=1,
            user_id=customer.id,
            status="pending",
            contact_name="No Avatar",
            contact_phone="+5491122233344",
            channel="whatsapp",
            total_monetary=1000,
            currency="ARS",
        )
        db.session.add(order)
        db.session.commit()

        serialized = serialize_unified_order(order)

        assert serialized["customer_profile"]["avatar_url"] is None
        assert serialized["customer_profile"]["avatar_consent"] is False
        assert serialized["customer_profile"]["identity"]["fallback"] == "deterministic_identity_avatar"


def test_market_order_query_is_legacy_safe_for_deferred_columns():
    app = create_app(TestConfig)
    with app.app_context():
        statement = str(MarketOrder.query.statement)
        assert "contact_key" not in statement
        assert "session_id" not in statement


def test_market_order_legacy_safe_query_omits_deferred_columns_in_filtered_queries():
    app = create_app(TestConfig)
    with app.app_context():
        statement = str(MarketOrder.legacy_safe_query().filter_by(tenant_id=1, user_id=2).statement)
        assert "contact_key" not in statement
        assert "session_id" not in statement


def test_dedupe_unified_orders_prefers_market_order_mirror_for_conversational_checkout():
    orders = [
        {
            "source_model": "PedidoConversacional",
            "source_id": 42,
            "created_at": "2026-03-22T09:00:00",
            "external_refs": {},
            "metadata": {},
        },
        {
            "source_model": "PymePedido",
            "source_id": 99,
            "created_at": "2026-03-22T09:01:00",
            "external_refs": {"idempotency_key": "conv_order_42"},
            "metadata": {"idempotency_key": "conv_order_42"},
        },
        {
            "source_model": "MarketOrder",
            "source_id": 7,
            "created_at": "2026-03-22T09:02:00",
            "external_refs": {"provider": "pedido_conversacional", "order_id": "42"},
            "metadata": {},
        },
    ]

    deduped = dedupe_unified_orders(orders)

    assert len(deduped) == 1
    assert deduped[0]["source_model"] == "MarketOrder"
    assert deduped[0]["source_id"] == 7


def test_serialize_unified_order_exposes_assisted_marketplace_upload_contract():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        owner = User(email="owner-assisted@test.com", name="Owner Assisted", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        tenant = TenantProfile(slug="assisted-market", nombre="Assisted Market", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        catalog_candidates = [
            {
                "item": "clavos bolsa",
                "row": {"nombre": "clavos bolsa"},
                "candidates": [
                    {
                        "catalogo_item_id": 33,
                        "product_id": 33,
                        "sku": "CL-33",
                        "nombre": "Clavos punta paris",
                        "name": "Clavos punta paris",
                        "score": 0.71,
                        "confidence": "medium",
                        "reason": "Comparte terminos clave: clavos",
                    }
                ],
            }
        ]
        crm_order_draft = {
            "contract_version": "marketplace.crm_order_draft.v1",
            "reference": "pedido:manual",
            "summary": {"matched": 1, "unmatched": 1},
            "lines": [
                {"status": "catalog_matched", "catalog_item_id": 12, "source_name": "Chapa acanalada"},
                {"status": "needs_catalog_resolution", "source_name": "clavos bolsa"},
            ],
        }

        pedido = PedidoConversacional(
            tenant_id=tenant.id,
            user_id=owner.id,
            estado="confirmado",
            tipo="nota_de_pedido",
            origen="marketplace",
            monto_monetario=0,
            items=[
                {
                    "archivo_url": "https://cdn.test/pedido.jpg",
                    "archivo_nombre": "pedido.jpg",
                    "items_detectados": [
                        {
                            "catalogo_item_id": 12,
                            "nombre": "Chapa acanalada",
                            "cantidad": 6,
                            "precio_float": 1200,
                            "sku": "CH-01",
                        }
                    ],
                    "no_encontrados": [{"nombre": "clavos bolsa", "catalog_candidates": catalog_candidates[0]["candidates"]}],
                    "no_encontrados_labels": ["clavos bolsa"],
                    "catalog_candidates": catalog_candidates,
                    "customer_message": "Recibimos tu nota y armamos un borrador.",
                }
            ],
            metadata_payload={
                "contract_version": "marketplace.assisted_request.v1",
                "mode": "order_note_upload",
                "crm_state": "pending_operator_review",
                "request_kind": "quote_request",
                "request_kind_label": "pedido de cotizacion",
                "document_profile": {
                    "primary_intent": "create_quote",
                    "catalog_matching": True,
                    "input_mode": "file",
                },
                "contact": {"name": "Marcelo", "phone": "+5492613168608", "email": "marcelo@example.com", "notes": "retira por deposito"},
                "source": {
                    "channel": "marketplace",
                    "input_type": "jpg",
                    "archivo_url": "https://cdn.test/pedido.jpg",
                    "archivo_nombre": "pedido.jpg",
                    "extraction_error": "lectura parcial",
                },
                "match_summary": {"matched": 1, "unmatched": 1, "detected": 2, "needs_operator_review": True},
                "review_context": {
                    "primary_intent": "create_quote",
                    "review_reasons": ["items_sin_match_exacto"],
                    "recommended_channels": ["whatsapp", "email", "phone", "crm"],
                },
                "catalog_candidates": catalog_candidates,
                "row_errors": [{"index": 1, "reason": "cantidad_asumida"}],
                "crm_order_draft": crm_order_draft,
                "customer_next_steps": [{"id": "reply", "label": "Respuesta por canal", "status": "pending"}],
                "intake_experience": {
                    "contract_version": "marketplace.assisted_intake_experience.v1",
                    "render_as": "anonymous_assisted_marketplace_intake",
                    "title": "Pedido asistido por foto, papel o texto",
                    "anonymous_intake": True,
                    "catalog_matching": True,
                    "needs_operator_review": True,
                    "pipeline": [
                        {"id": "capture", "label": "Archivo o texto recibido", "status": "done"},
                        {"id": "catalog_match", "label": "Cruce con catalogo", "status": "pending_review"},
                    ],
                    "capabilities": [{"id": "handwritten_note_ocr", "label": "Notas manuscritas"}],
                },
                "next_actions": [{"id": "tracking", "reference": "pedido:1"}],
                "operator_intake_summary": {
                    "contract_version": "marketplace.operator_intake_summary.v1",
                    "objective": "Confirmar stock, precio, alternativas y convertir la nota en pedido o cotizacion.",
                    "target_module": "orders",
                    "recommended_next_step": "resolver_faltantes_y_responder",
                    "contact_state": "available",
                },
            },
        )

        serialized = serialize_unified_order(pedido)

        assert serialized["source_model"] == "PedidoConversacional"
        assert serialized["channel"] == "marketplace"
        assert serialized["total"] == 0
        assert serialized["items"][0]["name"] == "Chapa acanalada"
        assert serialized["items"][0]["quantity"] == 6
        assert serialized["items"][0]["price"] == 1200
        assert serialized["assisted_request"]["contract_version"] == "marketplace.assisted_request.v1"
        assert serialized["assisted_request"]["request_kind"] == "quote_request"
        assert serialized["assisted_request"]["request_kind_label"] == "pedido de cotizacion"
        assert serialized["assisted_request"]["document_profile"]["primary_intent"] == "create_quote"
        assert serialized["assisted_request"]["contact"]["phone"] == "+5492613168608"
        assert serialized["assisted_request"]["source"]["archivo_url"] == "https://cdn.test/pedido.jpg"
        assert serialized["assisted_request"]["match_summary"]["unmatched"] == 1
        assert serialized["assisted_request"]["review_context"]["review_reasons"] == ["items_sin_match_exacto"]
        assert serialized["assisted_request"]["customer_next_steps"][0]["id"] == "reply"
        assert serialized["assisted_request"]["intake_experience"]["contract_version"] == "marketplace.assisted_intake_experience.v1"
        assert serialized["assisted_request"]["intake_experience"]["pipeline"][1]["id"] == "catalog_match"
        assert serialized["assisted_request"]["row_errors"][0]["reason"] == "cantidad_asumida"
        assert serialized["assisted_request"]["extraction_error"] == "lectura parcial"
        assert serialized["assisted_request"]["unmatched_items"] == ["clavos bolsa"]
        assert serialized["assisted_request"]["crm_order_draft"] == crm_order_draft
        assert serialized["assisted_request"]["crm_handoff"]["draft_order"] == crm_order_draft
        assisted_candidates = serialized["assisted_request"]["catalog_candidates"]
        assert assisted_candidates[0]["item"] == "clavos bolsa"
        assert assisted_candidates[0]["candidates"][0]["catalogo_item_id"] == 33
        assert assisted_candidates[0]["candidates"][0]["score"] == 0.71
        assert assisted_candidates[0]["candidates"][0]["reason"] == "Comparte terminos clave: clavos"
        assert serialized["assisted_request"]["next_actions"][0]["id"] == "tracking"
        assert serialized["assisted_request"]["operator_pack"]["priority"] == "high"
        assert serialized["assisted_request"]["operator_pack"]["reference"] is None
        assert serialized["assisted_request"]["operator_intake_summary"]["target_module"] == "orders"
        assert serialized["assisted_request"]["operator_intake_summary"]["recommended_next_step"] == "resolver_faltantes_y_responder"
        assert "clavos bolsa" in serialized["assisted_request"]["operator_pack"]["suggested_reply"]
        assert serialized["assisted_request"]["operator_pack"]["contact_links"][0]["type"] == "whatsapp"
        assert any(
            task["id"] == "resolve_unmatched_items"
            for task in serialized["assisted_request"]["operator_pack"]["suggested_tasks"]
        )
        review_card = serialized["crm_review_card"]
        assert review_card["contract_version"] == "marketplace.crm_review_card.v1"
        assert review_card["reference"] == "pedido:manual"
        assert review_card["request_kind"] == "quote_request"
        assert review_card["request_kind_label"] == "pedido de cotizacion"
        assert review_card["status"] == "needs_review"
        assert review_card["priority"] == "high"
        assert review_card["primary_intent"] == "create_quote"
        assert review_card["needs_operator_review"] is True
        assert review_card["contact"]["phone"] == "+5492613168608"
        assert review_card["summary"] == {"matched": 1, "unmatched": 1}
        assert review_card["lines"][0]["source_name"] == "Chapa acanalada"
        assert review_card["unmatched_items"] == ["clavos bolsa"]
        assert review_card["catalog_candidates"][0]["item"] == "clavos bolsa"
        assert review_card["suggested_reply"] == serialized["assisted_request"]["operator_pack"]["suggested_reply"]
        assert review_card["suggested_tasks"][0]["id"] == "review_ocr_confidence"
        assert review_card["contact_links"][0]["type"] == "whatsapp"
        reply_action = next(action for action in review_card["operator_actions"] if action["id"] == "reply_customer")
        assert reply_action["href"].startswith("https://wa.me/5492613168608?text=")
        assert reply_action["channel"] == "whatsapp"
        assert reply_action["action_label"] == "Responder por WhatsApp"
        assert serialized["contact"]["phone"] == "+5492613168608"


def test_serialize_unified_order_uses_first_item_crm_order_draft_when_metadata_missing():
    app = create_app(TestConfig)
    with app.app_context():
        crm_order_draft = {
            "contract_version": "marketplace.crm_order_draft.v1",
            "reference": "pedido:item-only",
            "summary": {"matched": 0, "unmatched": 1},
            "lines": [{"status": "needs_catalog_resolution", "source_name": "Tornillos"}],
        }
        pedido = PedidoConversacional(
            tenant_id=1,
            user_id=2,
            estado="confirmado",
            tipo="nota_de_pedido",
            origen="marketplace",
            monto_monetario=0,
            items=[
                {
                    "crm_order_draft": crm_order_draft,
                    "items_detectados": [],
                }
            ],
            metadata_payload={
                "contract_version": "marketplace.assisted_request.v1",
                "mode": "order_note_upload",
                "crm_handoff": {"target_module": "orders"},
            },
        )

        serialized = serialize_unified_order(pedido)

        assisted_request = serialized["assisted_request"]
        assert assisted_request["crm_order_draft"] == crm_order_draft
        assert assisted_request["crm_handoff"]["target_module"] == "orders"
        assert assisted_request["crm_handoff"]["draft_order"] == crm_order_draft
        assert serialized["crm_review_card"]["contract_version"] == "marketplace.crm_review_card.v1"
        assert serialized["crm_review_card"]["reference"] == "pedido:item-only"
        assert serialized["crm_review_card"]["lines"][0]["source_name"] == "Tornillos"


def test_market_order_legacy_safe_count_query_omits_deferred_columns():
    app = create_app(TestConfig)
    with app.app_context():
        statement = str(MarketOrder.legacy_safe_count_query(MarketOrder.tenant_id == 1, MarketOrder.user_id == 2).statement)
        assert "count(market_order.id)" in statement.lower()
        assert "contact_key" not in statement
        assert "session_id" not in statement
