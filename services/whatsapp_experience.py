from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from models import (
    CatalogoItem,
    EncEncuesta,
    EncRespuesta,
    MarketOrder,
    MunicipioPost,
    MunicipioTicket,
    PedidoConversacional,
    Promocion,
    PublicSurvey,
    PublicSurveyResponse,
    PymePedido,
    PymeTicket,
    TenantConfig,
    TenantProfile,
    TenantTicket,
    WhatsAppContactState,
    WhatsAppEnterpriseRule,
)
from services.commerce_contracts import build_checkout_experience_payload, payment_capabilities
from services.education_contracts import build_education_whatsapp_playbook, is_education_tenant
from services.plan_access import integration_access_payload
from services.realtime_voice_profiles import build_realtime_voice_capabilities
from services.audio_transcription_service import audio_translation_capabilities


WHATSAPP_EXPERIENCE_CONTRACT_VERSION = "whatsapp.experience.v1"


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None


def _normalized_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _safe_count(query) -> int:
    try:
        return int(query.count() or 0)
    except Exception:
        return 0


def _tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "plan": tenant.plan,
        "is_active": bool(getattr(tenant, "is_active", True)),
    }


def _tenant_cfg(tenant: TenantProfile) -> dict[str, Any]:
    return tenant.configuracion if isinstance(tenant.configuracion, dict) else {}


def _owner(tenant: TenantProfile):
    return tenant.pyme or tenant.municipio


def _latest_updated_at(*queries_and_columns: tuple[Any, Any]) -> str | None:
    latest: datetime | None = None
    for query, column in queries_and_columns:
        try:
            row = query.order_by(column.desc()).first()
        except Exception:
            row = None
        if not row:
            continue
        value = _normalized_datetime(getattr(row, column.key, None))
        if value is not None and (latest is None or value > latest):
            latest = value
    return _iso(latest)


def _whatsapp_number(tenant: TenantProfile, cfg: Mapping[str, Any]) -> str | None:
    owner = _owner(tenant)
    return (
        cfg.get("support_whatsapp")
        or cfg.get("whatsapp_phone")
        or cfg.get("whatsapp_sender_id")
        or getattr(tenant, "whatsapp_sender_id", None)
        or getattr(owner, "telefono", None)
    )


def _enterprise_rule_payload(tenant: TenantProfile) -> dict[str, Any]:
    rule = WhatsAppEnterpriseRule.query.filter_by(tenant_id=tenant.id).first()
    if not rule:
        return {
            "configured": False,
            "enforce_template_outside_24h": True,
            "max_outbound_per_hour": None,
            "quiet_hours": None,
            "blocked_keywords": [],
            "endpoint": "/api/admin/whatsapp/rules",
        }
    quiet_hours = None
    if rule.quiet_hours_start is not None or rule.quiet_hours_end is not None:
        quiet_hours = {"start": rule.quiet_hours_start, "end": rule.quiet_hours_end}
    return {
        "configured": True,
        "enforce_template_outside_24h": bool(rule.enforce_template_outside_24h),
        "max_outbound_per_hour": rule.max_outbound_per_hour,
        "quiet_hours": quiet_hours,
        "blocked_keywords": rule.blocked_keywords if isinstance(rule.blocked_keywords, list) else [],
        "endpoint": "/api/admin/whatsapp/rules",
    }


def _contact_window_payload(tenant: TenantProfile) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    active = _safe_count(
        WhatsAppContactState.query.filter(
            WhatsAppContactState.tenant_id == tenant.id,
            WhatsAppContactState.last_inbound_at >= since,
        )
    )
    total = _safe_count(WhatsAppContactState.query.filter_by(tenant_id=tenant.id))
    return {
        "active_24h": active,
        "known_contacts": total,
        "window_policy": "respond_freeform_inside_24h_use_templates_outside_window",
    }


def _content_modules_payload(tenant: TenantProfile) -> dict[str, Any]:
    owner = _owner(tenant)
    owner_id = getattr(owner, "id", None)
    catalog_count = _safe_count(CatalogoItem.query.filter_by(tenant_id=tenant.id))
    catalog_with_images = _safe_count(CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id, CatalogoItem.imagen_url.isnot(None)))
    legacy_surveys = EncEncuesta.query.filter_by(tenant_id=tenant.id)
    public_surveys = PublicSurvey.query.filter_by(tenant_id=tenant.id)
    public_survey_ids = [survey.id for survey in public_surveys.all()]
    public_responses = 0
    if public_survey_ids:
        public_responses = _safe_count(PublicSurveyResponse.query.filter(PublicSurveyResponse.survey_id.in_(public_survey_ids)))

    posts_by_type = {}
    for tipo in ["noticia", "evento", "informacion", "promocion", "promocionar"]:
        if owner_id:
            posts_by_type[tipo] = _safe_count(
                MunicipioPost.query.filter_by(municipio_id=owner_id, tipo_post=tipo)
            )
        else:
            posts_by_type[tipo] = 0

    promo_count = 0
    if owner_id:
        promo_count = _safe_count(Promocion.query.filter_by(pyme_user_id=owner_id))
    product_promos = _safe_count(CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id, CatalogoItem.promocion_info.isnot(None)))

    return {
        "catalog": {
            "enabled": catalog_count > 0,
            "items": catalog_count,
            "items_with_images": catalog_with_images,
            "image_coverage_rate": round((catalog_with_images / catalog_count) * 100, 2) if catalog_count else 100.0,
            "endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "bulk_import_endpoint": "/api/admin/catalogo/importar",
        },
        "surveys_votings": {
            "enabled": _safe_count(legacy_surveys) + _safe_count(public_surveys) > 0,
            "legacy_surveys": _safe_count(legacy_surveys),
            "public_surveys": len(public_survey_ids),
            "responses": _safe_count(EncRespuesta.query.filter_by(tenant_id=tenant.id)) + public_responses,
            "endpoint": "/api/v2/surveys",
            "draft_endpoint": "/api/v2/surveys/draft",
        },
        "news_events": {
            "enabled": any(value > 0 for value in posts_by_type.values()),
            "by_type": posts_by_type,
            "endpoint": "/api/municipal/posts",
        },
        "promotions": {
            "enabled": promo_count + product_promos + posts_by_type.get("promocion", 0) + posts_by_type.get("promocionar", 0) > 0,
            "promotions": promo_count,
            "product_promos": product_promos,
            "post_promos": posts_by_type.get("promocion", 0) + posts_by_type.get("promocionar", 0),
            "endpoint": "/api/whatsapp/promocionar",
        },
        "links": {
            "enabled": True,
            "sources": ["tenant.configuracion.links", "tenant_config:key=links", "catalog.external_url", "posts.enlace"],
            "endpoint": f"/api/admin/tenants/{tenant.slug}/config",
        },
    }


def _tracking_modules_payload(tenant: TenantProfile) -> dict[str, Any]:
    open_states = {"nuevo", "open", "pendiente", "in_progress", "en_proceso", "waiting_customer", "abierto"}
    ticket_count = _safe_count(TenantTicket.query.filter_by(tenant_id=tenant.id))
    municipio_count = _safe_count(MunicipioTicket.query.filter_by(tenant_id=tenant.id))
    pyme_ticket_count = _safe_count(PymeTicket.query.filter_by(tenant_id=tenant.id))
    open_ticket_count = (
        _safe_count(TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id, TenantTicket.estado.in_(list(open_states))))
        + _safe_count(MunicipioTicket.query.filter(MunicipioTicket.tenant_id == tenant.id, MunicipioTicket.estado.in_(list(open_states))))
        + _safe_count(PymeTicket.query.filter(PymeTicket.tenant_id == tenant.id, PymeTicket.estado.in_(list(open_states))))
    )
    order_count = (
        _safe_count(PedidoConversacional.query.filter_by(tenant_id=tenant.id))
        + _safe_count(MarketOrder.query.filter_by(tenant_id=tenant.id))
        + _safe_count(PymePedido.query.filter_by(tenant_id=tenant.id))
    )
    latest_ticket = _latest_updated_at(
        (TenantTicket.query.filter_by(tenant_id=tenant.id), TenantTicket.updated_at),
        (MunicipioTicket.query.filter_by(tenant_id=tenant.id), MunicipioTicket.fecha),
        (PymeTicket.query.filter_by(tenant_id=tenant.id), PymeTicket.fecha),
    )

    return {
        "claims": {
            "enabled": ticket_count + municipio_count + pyme_ticket_count > 0,
            "total": ticket_count + municipio_count + pyme_ticket_count,
            "open": open_ticket_count,
            "experience_endpoint": "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}",
            "public_status_endpoint": "/tickets/public/status",
            "public_status_alias": "/api/tickets/public/status",
            "tracking_page_template": "/tracking/claim/{nro_ticket}",
            "latest_activity_at": latest_ticket,
        },
        "orders": {
            "enabled": order_count > 0,
            "total": order_count,
            "experience_endpoint": "/api/public/tracking/experience?kind=order&code={code}",
            "tracking_page_template": "/tracking/order/{nro_pedido}",
            "payment_status_endpoint": "/api/v2/payments/status",
        },
        "courier_style_map": {
            "enabled": True,
            "render_contract": {
                "type": "tracking_map_timeline",
                "layers": ["origin", "current_status", "destination_or_claim_location", "timeline_events"],
                "animations": ["pulse_current_step", "route_progress", "status_transition"],
                "fallback_when_no_coordinates": "timeline_only",
            },
            "realtime_sources": ["ticket.status.changed", "new_chat_message", "order_event", "ticket_realtime_state"],
        },
        "milestones": {
            "claim": ["recibido", "validando", "asignado", "en_proceso", "resuelto", "cerrado"],
            "order": ["recibido", "confirmado", "pendiente_pago", "pagado", "preparando", "en_camino", "entregado"],
        },
    }


def _conversation_intelligence_payload(tenant: TenantProfile, cfg: Mapping[str, Any], app_config: Mapping[str, Any] | None) -> dict[str, Any]:
    voice = build_realtime_voice_capabilities(tenant, cfg, app_config)
    return {
        "llm_strategy": {
            "primary": "llm_orchestrated_actions",
            "python_role": "execute_validate_persist",
            "avoid_keyword_only_flows": True,
        },
        "inputs": {
            "text": {"enabled": True, "notes": "Incluye emojis y lenguaje natural."},
            "emoji": {"enabled": True, "intent_hint": "normalizar sin perder tono del usuario"},
            "location": {"enabled": True, "uses": ["claim_location", "delivery_address", "school_location"]},
            "image": {"enabled": True, "uses": ["claim_evidence", "product_photo", "invoice_or_order_note", "school_certificate"]},
            "audio_note": {
                "enabled": True,
                "mode": "transcribe_then_reason",
                "upgrade_path": "realtime_voice_for_calls_native_speech_to_speech",
                "translation": audio_translation_capabilities(),
            },
            "file_pdf_doc": {"enabled": True, "uses": ["catalog_import", "invoice", "order_note", "school_document"]},
            "video": {
                "enabled": True,
                "receive_as_attachment": True,
                "analysis_ready": False,
                "reason_code": "video_intelligence_pipeline_pending",
            },
        },
        "voice_calls": {
            "enabled": bool(cfg.get("realtime_voice_enabled", True)),
            "capabilities": voice,
            "best_current_architecture": {
                "browser": "OpenAI Realtime over WebRTC",
                "server": "OpenAI Realtime over WebSocket",
                "phone": "Twilio Media Streams or SIP bridge into Realtime",
            },
        },
        "business_actions": [
            "crear_reclamo",
            "consultar_estado_reclamo",
            "crear_ticket",
            "consultar_estado_pedido",
            "crear_pedido",
            "consultar_catalogo",
            "crear_checkout_seguro",
            "consultar_estado_pago",
            "confirmar_pago_por_webhook",
            "registrar_encuesta",
            "derivar_humano",
        ],
    }


def _admin_panel_payload(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "tenant_config": f"/api/admin/tenants/{tenant.slug}/config",
        "whatsapp_rules": "/api/admin/whatsapp/rules",
        "whatsapp_test": "/api/notifications/whatsapp/test",
        "whatsapp_funnel": "/admin/analytics/whatsapp-funnel",
        "inbox": "/api/v2/inbox/omnichannel",
        "tickets": "/api/v2/tickets",
        "employee_coverage": "/api/v2/employee-coverage",
        "notifications": "/api/v2/notifications/hooks",
        "catalog": f"/api/admin/tenants/{tenant.slug}/catalog/items",
        "surveys": "/api/v2/surveys",
        "analytics": "/api/v2/analytics/operations/dashboard",
        "education_whatsapp": "/api/v1/education/whatsapp/playbook" if is_education_tenant(tenant) else None,
    }


def build_whatsapp_experience(
    tenant: TenantProfile,
    *,
    app_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _tenant_cfg(tenant)
    number = _whatsapp_number(tenant, cfg)
    integration_access = integration_access_payload(tenant)
    tenant_config_links = _safe_count(TenantConfig.query.filter_by(tenant_id=tenant.id, key="links"))
    channel_configured = bool(number)
    channel_ready = channel_configured and bool(integration_access.get("enabled"))
    content = _content_modules_payload(tenant)
    tracking = _tracking_modules_payload(tenant)
    payment = payment_capabilities(tenant)
    checkout_experience = build_checkout_experience_payload(
        tenant,
        channel="whatsapp",
        gateway=payment.get("gateway"),
        mercadopago_ready=payment.get("mercadopago_ready"),
    )
    channel_reason = None
    if not channel_ready:
        channel_reason = (
            "plan_full_required"
            if not integration_access.get("enabled")
            else "whatsapp_number_not_configured"
        )
    channel = {
        "provider": "twilio_whatsapp",
        "enabled": channel_ready,
        "configured": channel_configured,
        "production_enabled": channel_ready,
        "number": number,
        "webhook": "/webhook/whatsapp",
        "status_webhook": "/twilio/whatsapp/status",
        "reason_code": channel_reason,
        "access": integration_access,
        "provider_readiness": [
            {
                "id": "plan_full",
                "label": "Plan Full activo",
                "status": "done" if integration_access.get("enabled") else "required",
            },
            {
                "id": "sender_number",
                "label": "Numero WhatsApp configurado",
                "status": "done" if channel_configured else "required",
            },
            {
                "id": "inbound_webhook",
                "label": "Webhook inbound",
                "status": "ready" if channel_ready else "blocked",
                "endpoint": "/webhook/whatsapp",
            },
            {
                "id": "status_webhook",
                "label": "Webhook de estados",
                "status": "ready" if channel_ready else "blocked",
                "endpoint": "/twilio/whatsapp/status",
            },
            {
                "id": "test_message",
                "label": "Mensaje de prueba",
                "status": "ready" if channel_ready else "blocked",
                "endpoint": "/api/notifications/whatsapp/test",
            },
        ],
    }
    if channel_ready:
        channel.update(
            {
                "test_endpoint": "/api/notifications/whatsapp/test",
                "test_method": "POST",
                "test_label": "Probar canal",
                "test_payload_hint": {
                    "recipient": "whatsapp:+549...",
                    "body": "Mensaje de prueba",
                    "metadata": {},
                },
            }
        )

    return {
        "contract_version": WHATSAPP_EXPERIENCE_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant),
        "channel": channel,
        "enterprise_rules": _enterprise_rule_payload(tenant),
        "contact_window": _contact_window_payload(tenant),
        "conversation_intelligence": _conversation_intelligence_payload(tenant, cfg, app_config),
        "content_modules": {
            **content,
            "links": {**content["links"], "tenant_config_links": tenant_config_links},
        },
        "tracking": tracking,
        "commerce": {
            "payments": payment,
            "checkout_experience": checkout_experience,
            "customer_policy": {
                "payment_capture": "external_secure_webview",
                "paid_state_source": "server_to_server_webhook",
                "card_data_in_chat": False,
                "client_return_trusted": False,
            },
        },
        "admin_panel": _admin_panel_payload(tenant),
        "education": {
            "enabled": is_education_tenant(tenant),
            "whatsapp_playbook": build_education_whatsapp_playbook(tenant) if is_education_tenant(tenant) else None,
        },
        "frontend_contract": {
            "render_as": "whatsapp_operations_hub",
            "primary_refresh_seconds": 30,
            "respect_access_lock": True,
            "show_provider_readiness": True,
            "recommended_views": [
                "channel_health",
                "conversation_capabilities",
                "content_modules",
                "claim_order_tracking",
                "commerce_checkout",
                "voice_realtime",
                "enterprise_rules",
            ],
            "empty_state_behavior": "show_setup_checklist_and_safe_degradation",
        },
    }
