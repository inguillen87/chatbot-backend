from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from models import (
    CatalogoItem,
    EncEncuesta,
    EncRespuesta,
    MarketOrder,
    MessageTemplateRegistry,
    MunicipioPost,
    MunicipioTicket,
    NotificationTemplate,
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
from services.huggingface_ai_insights import build_whatsapp_ai_runtime_contract
from services.plan_access import integration_access_payload
from services.realtime_voice_profiles import build_realtime_voice_capabilities
from services.audio_transcription_service import audio_translation_capabilities


WHATSAPP_EXPERIENCE_CONTRACT_VERSION = "whatsapp.experience.v1"

APPROVED_TEMPLATE_STATUSES = {"approved", "active", "ready", "published", "online"}
PENDING_TEMPLATE_STATUSES = {"draft", "pending", "submitted", "in_review", "review", "twilio_review"}
CHATBOC_TEMPLATE_FRIENDLY_NAMES = {
    "welcome_menu": "chatboc_welcome_menu_v2",
    "case_created": "chatboc_gov_claim_created_v2",
    "order_checkout": "chatboc_order_checkout_v1",
    "payment_confirmed": "chatboc_payment_confirmed_v1",
    "survey_invite": "chatboc_survey_invite_v2",
    "human_handoff": "chatboc_handoff_v1",
    "pyme_order_ready": "chatboc_pyme_order_ready_v1",
    "pyme_payment_link": "chatboc_pyme_payment_link_v1",
    "pyme_delivery_update": "chatboc_pyme_delivery_update_v1",
    "pyme_quote_followup": "chatboc_pyme_quote_followup_v1",
    "pyme_catalog_invite": "chatboc_pyme_catalog_invite_v1",
    "school_payment_due": "chatboc_school_payment_due_v2",
    "school_receipt_ready": "chatboc_school_receipt_ready_v1",
    "school_certificate_ready": "chatboc_school_certificate_ready_v1",
    "school_family_case_created": "chatboc_school_family_case_created_v1",
    "school_event_reminder": "chatboc_school_event_reminder_v1",
    "gov_claim_created": "chatboc_gov_claim_created_v2",
    "gov_claim_status_update": "chatboc_gov_claim_status_update_v1",
    "gov_turn_reminder": "chatboc_gov_turn_reminder_v1",
    "gov_document_ready": "chatboc_gov_document_ready_v1",
    "gov_survey_invite": "chatboc_gov_survey_invite_v1",
    "gov_claim_sla": "gobiernos_reclamo_sla",
    "gov_tax_due": "gobiernos_tasa_vencimiento",
    "gov_turn_confirmation": "gobiernos_turno_confirmacion",
    "gov_public_announcement": "gobiernos_comunicado_segmentado",
    "gov_procedure_status": "gobiernos_tramite_estado",
    "school_tuition_due": "colegios_cuota_vencimiento",
    "school_admin_turn": "colegios_turno_administracion",
    "clinic_turn_reminder": "clinicas_turno_recordatorio",
    "club_fee_due": "clubes_cuota_social",
    "club_reservation": "clubes_reserva_disciplina",
    "condo_expense_due": "consorcios_expensas_vencimiento",
    "condo_claim_sla": "consorcios_reclamo_sla",
    "condo_receipt_received": "consorcios_comprobante_recibido",
    "condo_amenity_booking": "consorcios_reserva_amenity",
    "entrepreneur_order_confirmed": "emprendedores_pedido_confirmado",
    "entrepreneur_receipt_review": "emprendedores_comprobante_revision",
    "standard_handoff": "standard_handoff_humano",
    "opt_in": "message_opt_in",
    "navigation": "copy_navegacion",
    "customer_care_greeting": "customer_care_greeting_template",
    "customer_care_help_center": "customer_care_help_center_template",
    "customer_support_routing": "customer_support_routing_template",
    "order_tracking_list": "notification_order_tracking",
    "promo_media": "promocionar",
    "survey_banner": "bannerencu",
    "juni_welcome_media": "saludo_inicial_juni",
    "turnero_payment_webview": "turnero_pago_seguro_webview",
    "turnero_pyme_order_webview": "turnero_pyme_pedido_webview",
    "turnero_school_admission_webview": "turnero_colegio_admision_webview",
    "turnero_government_procedure_webview": "turnero_gobierno_tramite_webview",
    "turnero_support_case_webview": "turnero_soporte_caso_webview",
}

OPERATIONAL_TEMPLATE_GROUPS = {
    "entry_and_navigation": {
        "label": "Entrada, opt-in y menus",
        "purpose": "Dar bienvenida, capturar consentimiento y ordenar la conversacion sin salir de WhatsApp.",
        "items": [
            ("welcome_menu", "menu", "widget_and_whatsapp", ["show_main_menu", "route_vertical"]),
            ("opt_in", "consent", "whatsapp", ["capture_opt_in"]),
            ("navigation", "menu", "whatsapp", ["back", "menu", "cancel"]),
            ("customer_care_greeting", "support", "whatsapp", ["open_support"]),
            ("customer_care_help_center", "support", "webview", ["open_help_center"]),
            ("customer_support_routing", "support", "whatsapp", ["route_support_queue"]),
        ],
    },
    "commerce_and_payments": {
        "label": "Pedidos, pagos y seguimiento comercial",
        "purpose": "Cotizar, crear pedidos, cobrar y seguir entregas usando botones y webviews seguros.",
        "items": [
            ("pyme_order_ready", "order", "webview", ["review_order", "pay_order"]),
            ("pyme_payment_link", "payment", "webview", ["pay_securely"]),
            ("order_checkout", "checkout", "webview", ["checkout"]),
            ("payment_confirmed", "payment", "whatsapp", ["show_receipt", "track_order"]),
            ("pyme_delivery_update", "delivery", "whatsapp", ["track_order"]),
            ("pyme_quote_followup", "quote", "webview", ["review_quote"]),
            ("pyme_catalog_invite", "catalog", "webview", ["open_catalog", "add_to_cart"]),
            ("entrepreneur_order_confirmed", "order", "whatsapp", ["confirm_order"]),
            ("entrepreneur_receipt_review", "payment", "whatsapp", ["review_receipt"]),
            ("order_tracking_list", "tracking", "whatsapp", ["track_order"]),
            ("promo_media", "marketing", "whatsapp", ["open_promotion"]),
        ],
    },
    "education": {
        "label": "Colegios y familias",
        "purpose": "Resolver cuotas, certificados, tramites familiares, turnos y avisos escolares.",
        "items": [
            ("school_payment_due", "payment", "webview", ["pay_fee"]),
            ("school_tuition_due", "payment", "webview", ["pay_fee"]),
            ("school_receipt_ready", "receipt", "webview", ["download_receipt"]),
            ("school_certificate_ready", "certificate", "webview", ["download_certificate"]),
            ("school_family_case_created", "case", "whatsapp", ["track_case", "attach_info"]),
            ("school_event_reminder", "event", "webview", ["open_event"]),
            ("school_admin_turn", "appointment", "whatsapp", ["confirm_turn", "reschedule"]),
            ("turnero_school_admission_webview", "admission", "webview", ["start_admission"]),
        ],
    },
    "government": {
        "label": "Gobiernos y municipios",
        "purpose": "Registrar reclamos, turnos, tasas, documentos, comunicados y participacion ciudadana.",
        "items": [
            ("gov_claim_created", "claim", "webview", ["track_claim"]),
            ("gov_claim_sla", "claim", "whatsapp", ["confirm_claim", "edit_claim", "cancel_claim"]),
            ("gov_claim_status_update", "claim", "whatsapp", ["track_claim"]),
            ("gov_procedure_status", "procedure", "whatsapp", ["track_procedure"]),
            ("gov_tax_due", "payment", "webview", ["pay_tax"]),
            ("gov_turn_confirmation", "appointment", "whatsapp", ["confirm_turn", "reschedule"]),
            ("gov_turn_reminder", "appointment", "webview", ["open_turn"]),
            ("gov_document_ready", "document", "webview", ["download_document"]),
            ("gov_public_announcement", "announcement", "webview", ["open_announcement"]),
            ("gov_survey_invite", "survey", "webview", ["vote"]),
            ("survey_invite", "survey", "webview", ["vote"]),
            ("survey_banner", "survey", "whatsapp", ["open_survey"]),
            ("juni_welcome_media", "media", "whatsapp", ["show_municipal_intro"]),
            ("turnero_government_procedure_webview", "procedure", "webview", ["start_procedure"]),
        ],
    },
    "appointments_and_services": {
        "label": "Turnos, clinicas y servicios",
        "purpose": "Confirmar turnos, derivar soporte y mantener la experiencia dentro del canal.",
        "items": [
            ("clinic_turn_reminder", "appointment", "whatsapp", ["confirm_turn", "reschedule"]),
            ("turnero_support_case_webview", "support", "webview", ["open_support_case"]),
            ("standard_handoff", "handoff", "whatsapp", ["human_handoff"]),
            ("human_handoff", "handoff", "whatsapp", ["human_handoff"]),
        ],
    },
    "clubs_and_consorcios": {
        "label": "Clubes, consorcios y comunidades",
        "purpose": "Cobrar cuotas/expensas, gestionar reservas, reclamos y comprobantes.",
        "items": [
            ("club_fee_due", "payment", "webview", ["pay_fee"]),
            ("club_reservation", "reservation", "whatsapp", ["book_activity"]),
            ("condo_expense_due", "payment", "webview", ["pay_expense"]),
            ("condo_claim_sla", "claim", "whatsapp", ["track_claim"]),
            ("condo_receipt_received", "receipt", "whatsapp", ["review_receipt"]),
            ("condo_amenity_booking", "reservation", "whatsapp", ["book_amenity"]),
        ],
    },
    "webviews": {
        "label": "Webviews seguros",
        "purpose": "Resolver pagos, pedidos, tramites y soporte con pantallas firmadas y confirmacion server-to-server.",
        "items": [
            ("turnero_payment_webview", "payment", "webview", ["pay_securely"]),
            ("turnero_pyme_order_webview", "order", "webview", ["create_order"]),
            ("turnero_school_admission_webview", "education", "webview", ["start_admission"]),
            ("turnero_government_procedure_webview", "procedure", "webview", ["start_procedure"]),
            ("turnero_support_case_webview", "support", "webview", ["open_support_case"]),
        ],
    },
}


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


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
        "huggingface_ai": build_whatsapp_ai_runtime_contract(),
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


def _registered_template_map(tenant: TenantProfile) -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}

    registry_rows = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        channel="whatsapp",
    ).all()
    for row in registry_rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        templates[_lower(row.name)] = {
            "source": "message_template_registry",
            "id": row.id,
            "name": row.name,
            "language": row.language,
            "category": row.category,
            "status": _lower(row.status or "draft"),
            "content_sid": row.content_sid,
            "external_template_id": row.external_template_id,
            "body_preview": row.body_preview,
            "components": row.components if isinstance(row.components, list) else [],
            "metadata": metadata,
            "last_sync_at": _iso(row.last_sync_at),
        }

    notification_rows = NotificationTemplate.query.filter_by(
        tenant_id=tenant.id,
        channel="whatsapp",
    ).all()
    for row in notification_rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        key = _lower(row.key)
        templates.setdefault(
            key,
            {
                "source": "notification_template",
                "id": row.id,
                "name": row.key,
                "language": str(metadata.get("language") or "es"),
                "category": metadata.get("category"),
                "status": _lower(metadata.get("status") or ("active" if row.is_active else "inactive")),
                "content_sid": metadata.get("content_sid"),
                "external_template_id": metadata.get("external_template_id"),
                "body_preview": row.body_template,
                "components": metadata.get("components") if isinstance(metadata.get("components"), list) else [],
                "metadata": metadata,
                "last_sync_at": None,
            },
        )

    return templates


def _template_lookup_candidates(template_id: str) -> list[str]:
    base = _lower(template_id)
    candidates = [base]
    friendly_name = CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(base)
    if friendly_name:
        candidates.append(_lower(friendly_name))
    candidates.extend([f"chatboc_{base}_v{version}" for version in range(1, 5)])
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _pick_registered_template(
    templates: Mapping[str, dict[str, Any]],
    template_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    for candidate in _template_lookup_candidates(template_id):
        template = templates.get(candidate)
        if template:
            return candidate, template

    prefix = f"chatboc_{_lower(template_id)}_v"
    matches = [
        (name, template)
        for name, template in templates.items()
        if name.startswith(prefix)
    ]
    if not matches:
        return None, None
    approved = [
        (name, template)
        for name, template in matches
        if _lower(template.get("status")) in APPROVED_TEMPLATE_STATUSES
    ]
    return sorted(approved or matches, key=lambda item: item[0], reverse=True)[0]


def _template_status(templates: Mapping[str, dict[str, Any]], template_id: str) -> dict[str, Any]:
    resolved_name, template = _pick_registered_template(templates, template_id)
    if not template:
        return {
            "configured": False,
            "approved": False,
            "status": "missing",
            "source": None,
            "resolved_name": None,
            "expected_friendly_name": CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(template_id)),
            "content_sid": None,
            "external_template_id": None,
        }

    status = _lower(template.get("status"))
    return {
        "configured": True,
        "approved": status in APPROVED_TEMPLATE_STATUSES,
        "pending": status in PENDING_TEMPLATE_STATUSES,
        "status": status or "unknown",
        "source": template.get("source"),
        "resolved_name": resolved_name or template.get("name"),
        "expected_friendly_name": CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(template_id)),
        "content_sid": template.get("content_sid"),
        "external_template_id": template.get("external_template_id"),
        "last_sync_at": template.get("last_sync_at"),
    }


def _template_catalog_item(
    templates: Mapping[str, dict[str, Any]],
    template_id: str,
    *,
    stage: str,
    entrypoint: str,
    actions: list[str],
) -> dict[str, Any]:
    friendly_name = CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(template_id))
    status_payload = _template_status(templates, template_id)
    return {
        "id": template_id,
        "friendly_name": friendly_name,
        "stage": stage,
        "entrypoint": entrypoint,
        "requires_webview": entrypoint == "webview",
        "in_chat_action": entrypoint in {"whatsapp", "widget_and_whatsapp"},
        "widget_action": entrypoint in {"widget", "widget_and_whatsapp", "webview"},
        "actions": actions,
        "status": status_payload,
    }


def _operational_template_groups_payload(
    templates: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    total = 0
    configured = 0
    approved = 0
    webviews = 0

    for group_id, group in OPERATIONAL_TEMPLATE_GROUPS.items():
        items = []
        for template_id, stage, entrypoint, actions in group["items"]:
            item = _template_catalog_item(
                templates,
                template_id,
                stage=stage,
                entrypoint=entrypoint,
                actions=actions,
            )
            items.append(item)
            total += 1
            configured += 1 if item["status"].get("configured") else 0
            approved += 1 if item["status"].get("approved") else 0
            webviews += 1 if item.get("requires_webview") else 0

        groups[group_id] = {
            "label": group["label"],
            "purpose": group["purpose"],
            "items": items,
            "summary": {
                "total": len(items),
                "configured": sum(1 for item in items if item["status"].get("configured")),
                "approved": sum(1 for item in items if item["status"].get("approved")),
                "webviews": sum(1 for item in items if item.get("requires_webview")),
            },
        }

    return {
        "groups": groups,
        "summary": {
            "total_catalog_items": total,
            "configured": configured,
            "approved": approved,
            "missing": total - configured,
            "webviews": webviews,
        },
    }


def _template_blueprint_payload(
    tenant: TenantProfile,
    *,
    channel_ready: bool,
    integration_access: Mapping[str, Any],
) -> dict[str, Any]:
    templates = _registered_template_map(tenant)
    required_templates = [
        {
            "id": "welcome_menu",
            "name": "welcome_menu",
            "category": "UTILITY",
            "language": "es",
            "purpose": "Abrir una conversacion clara con menu inicial por vertical.",
            "body": "Hola {{1}}, soy {{2}}. Te puedo ayudar con {{3}}. Elegi una opcion para continuar.",
            "variables": ["contact_name", "tenant_name", "main_capabilities"],
            "components": ["body", "quick_reply_or_list"],
            "suggested_actions": ["open_claim", "open_order", "open_survey", "human_handoff"],
        },
        {
            "id": "case_created",
            "name": "case_created",
            "category": "UTILITY",
            "language": "es",
            "purpose": "Confirmar reclamos, tickets o tramites con seguimiento publico.",
            "body": "Tu caso {{1}} fue creado. Estado: {{2}}. Podes seguirlo aca: {{3}}",
            "variables": ["case_code", "status", "tracking_url"],
            "components": ["body", "cta_url"],
        },
        {
            "id": "order_checkout",
            "name": "order_checkout",
            "category": "UTILITY",
            "language": "es",
            "purpose": "Enviar resumen de pedido y checkout seguro sin pedir datos de tarjeta por chat.",
            "body": "Tu pedido {{1}} esta listo. Total: {{2}}. Paga de forma segura desde este enlace: {{3}}",
            "variables": ["order_code", "total", "checkout_url"],
            "components": ["body", "cta_webview"],
        },
        {
            "id": "payment_confirmed",
            "name": "payment_confirmed",
            "category": "UTILITY",
            "language": "es",
            "purpose": "Avisar pago acreditado usando el estado confirmado por webhook.",
            "body": "Pago acreditado para {{1}}. El pedido queda confirmado y listo para seguimiento: {{2}}",
            "variables": ["order_code", "tracking_url"],
            "components": ["body", "cta_url"],
        },
        {
            "id": "survey_invite",
            "name": "survey_invite",
            "category": "UTILITY",
            "language": "es",
            "purpose": "Invitar a votar o responder encuestas operativas vinculadas al servicio.",
            "body": "{{1}} te invita a responder: {{2}}. Participa aca: {{3}}",
            "variables": ["tenant_name", "survey_title", "survey_url"],
            "components": ["body", "cta_url"],
            "policy_note": "Usar MARKETING si la encuesta no esta vinculada a una relacion de servicio.",
        },
        {
            "id": "human_handoff",
            "name": "human_handoff",
            "category": "UTILITY",
            "language": "es",
            "purpose": "Derivar a un operador con contexto y evitar respuestas repetitivas.",
            "body": "Derivamos tu consulta {{1}} al equipo. Un operador va a responderte por este canal.",
            "variables": ["case_or_order_code"],
            "components": ["body"],
        },
    ]

    vertical_templates = {
        "pyme": [
            {
                "id": "pyme_order_ready",
                "name": "pyme_order_ready",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Confirmar pedido armado con resumen, total y acceso a pago seguro.",
                "body": "Tu pedido {{1}} esta listo. Total {{2}}. Revisalo y pagalo aca: {{3}}",
                "variables": ["order_code", "total", "checkout_url"],
                "components": ["body", "cta_webview"],
            },
            {
                "id": "pyme_payment_link",
                "name": "pyme_payment_link",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Enviar link de pago desde una conversacion comercial ya iniciada.",
                "body": "Hola {{1}}, tu link de pago de {{2}} esta disponible: {{3}}",
                "variables": ["contact_name", "amount", "payment_url"],
                "components": ["body", "cta_webview"],
            },
            {
                "id": "pyme_delivery_update",
                "name": "pyme_delivery_update",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Actualizar estado de envio o retiro con tracking.",
                "body": "Actualizacion del pedido {{1}}: {{2}}. Seguimiento: {{3}}",
                "variables": ["order_code", "status", "tracking_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "pyme_quote_followup",
                "name": "pyme_quote_followup",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Continuar una cotizacion solicitada por el cliente.",
                "body": "Tu cotizacion {{1}} ya esta preparada. Podes revisarla aca: {{2}}",
                "variables": ["quote_code", "quote_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "pyme_catalog_invite",
                "name": "pyme_catalog_invite",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Abrir catalogo desde WhatsApp sin pedir datos sensibles.",
                "body": "Mira el catalogo actualizado de {{1}} aca: {{2}}",
                "variables": ["tenant_name", "catalog_url"],
                "components": ["body", "cta_url"],
            },
        ],
        "colegio": [
            {
                "id": "school_payment_due",
                "name": "school_payment_due",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Informar cuota o concepto pendiente con pago seguro.",
                "body": "{{1}}, tenes {{2}} pendiente por {{3}}. Podes pagarlo aca: {{4}}",
                "variables": ["family_name", "concept", "amount", "payment_url"],
                "components": ["body", "cta_webview"],
            },
            {
                "id": "school_receipt_ready",
                "name": "school_receipt_ready",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Enviar comprobante listo despues de confirmacion del gateway.",
                "body": "Comprobante {{1}} disponible para {{2}}. Descargalo aca: {{3}}",
                "variables": ["receipt_code", "student_name", "receipt_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "school_certificate_ready",
                "name": "school_certificate_ready",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Avisar certificado escolar listo para descargar o retirar.",
                "body": "El certificado de {{1}} esta listo. Seguimiento: {{2}}",
                "variables": ["student_name", "tracking_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "school_family_case_created",
                "name": "school_family_case_created",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Confirmar tramite o consulta familiar con numero de caso.",
                "body": "Creamos el caso {{1}} para {{2}}. Estado: {{3}}",
                "variables": ["case_code", "student_name", "status"],
                "components": ["body"],
            },
            {
                "id": "school_event_reminder",
                "name": "school_event_reminder",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Recordar evento escolar vinculado a la comunidad educativa.",
                "body": "Recordatorio de {{1}}: {{2}}. Mas informacion: {{3}}",
                "variables": ["event_title", "event_date", "event_url"],
                "components": ["body", "cta_url"],
            },
        ],
        "gobierno": [
            {
                "id": "gov_claim_created",
                "name": "gov_claim_created",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Confirmar reclamo municipal con codigo publico y seguimiento.",
                "body": "Tu reclamo {{1}} fue registrado. Categoria: {{2}}. Seguimiento: {{3}}",
                "variables": ["claim_code", "category", "tracking_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "gov_claim_status_update",
                "name": "gov_claim_status_update",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Actualizar estado del reclamo sin abrir nuevos casos duplicados.",
                "body": "Actualizacion del reclamo {{1}}: {{2}}. Detalle: {{3}}",
                "variables": ["claim_code", "status", "tracking_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "gov_turn_reminder",
                "name": "gov_turn_reminder",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Recordar turno municipal solicitado por el ciudadano.",
                "body": "Recordatorio: turno {{1}} para {{2}} el {{3}}. Ver detalle: {{4}}",
                "variables": ["turn_code", "office", "date_time", "turn_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "gov_document_ready",
                "name": "gov_document_ready",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Avisar que un certificado o documento esta listo.",
                "body": "Tu documento {{1}} esta listo. Podes consultarlo aca: {{2}}",
                "variables": ["document_code", "document_url"],
                "components": ["body", "cta_url"],
            },
            {
                "id": "gov_survey_invite",
                "name": "gov_survey_invite",
                "category": "UTILITY",
                "language": "es",
                "purpose": "Invitar a participacion ciudadana vinculada a servicio publico.",
                "body": "{{1}} te invita a participar: {{2}}. Responde aca: {{3}}",
                "variables": ["tenant_name", "survey_title", "survey_url"],
                "components": ["body", "cta_url"],
            },
        ],
    }

    configured = 0
    approved = 0
    for item in required_templates:
        item["friendly_name"] = CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(item["id"]))
        status_payload = _template_status(templates, item["id"])
        item["status"] = status_payload
        configured += 1 if status_payload.get("configured") else 0
        approved += 1 if status_payload.get("approved") else 0

    vertical_configured = 0
    vertical_approved = 0
    for items in vertical_templates.values():
        for item in items:
            item["friendly_name"] = CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(item["id"]))
            status_payload = _template_status(templates, item["id"])
            item["status"] = status_payload
            vertical_configured += 1 if status_payload.get("configured") else 0
            vertical_approved += 1 if status_payload.get("approved") else 0

    tenant_vertical_tokens = {
        _lower(getattr(tenant, "tipo", "")),
        _lower(getattr(tenant, "vertical", "")),
        _lower(getattr(tenant, "subvertical", "")),
    }
    recommended_verticals: list[str] = []
    if {"pyme", "empresa", "empresas", "comercio"} & tenant_vertical_tokens:
        recommended_verticals.append("pyme")
    if {"colegio", "educacion", "education", "school"} & tenant_vertical_tokens or is_education_tenant(tenant):
        recommended_verticals.append("colegio")
    if {"municipio", "gobierno", "gov"} & tenant_vertical_tokens:
        recommended_verticals.append("gobierno")
    if not recommended_verticals:
        recommended_verticals = ["pyme", "colegio", "gobierno"]

    operational_catalog = _operational_template_groups_payload(templates)

    return {
        "provider": "twilio_content_api",
        "channel": "whatsapp",
        "enabled": bool(channel_ready),
        "required_templates": required_templates,
        "vertical_templates": vertical_templates,
        "operational_template_groups": operational_catalog["groups"],
        "recommended_verticals": recommended_verticals,
        "registry_summary": {
            "total_registered": len(templates),
            "required": len(required_templates),
            "configured": configured,
            "approved": approved,
            "missing": len(required_templates) - configured,
            "vertical_required": sum(len(items) for items in vertical_templates.values()),
            "vertical_configured": vertical_configured,
            "vertical_approved": vertical_approved,
            "operational_catalog_total": operational_catalog["summary"]["total_catalog_items"],
            "operational_configured": operational_catalog["summary"]["configured"],
            "operational_approved": operational_catalog["summary"]["approved"],
            "operational_missing": operational_catalog["summary"]["missing"],
            "operational_webviews": operational_catalog["summary"]["webviews"],
        },
        "twilio_content_types": {
            "transactional": ["twilio/text", "twilio/call-to-action", "twilio/quick-reply"],
            "menus": ["twilio/list-picker", "twilio/quick-reply"],
            "checkout": ["twilio/call-to-action"],
        },
        "template_creation_payload_hint": {
            "language": "es",
            "approval_categories": ["UTILITY", "MARKETING", "AUTHENTICATION"],
            "variables_format": "{{1}}, {{2}}, {{3}}",
            "sample_values_required": True,
            "submit_to_meta_after_create": True,
        },
        "policy": {
            "requires_meta_approval_outside_24h": True,
            "variables_must_be_sequential": True,
            "authentication_templates_disallow_custom_variables": True,
            "use_utility_for_transactional": True,
            "use_marketing_for_promotions": True,
            "buttons_use_cta_webview_or_quick_reply": True,
            "freeform_allowed_inside_24h": True,
            "outside_24h_allowed": bool(channel_ready and approved > 0),
            "production_send_allowed": bool(channel_ready),
            "respect_integration_access_lock": True,
            "access_enabled": bool(integration_access.get("enabled")),
        },
        "endpoints": {
            "templates_admin": "/api/admin/templates",
            "rules_admin": "/api/admin/whatsapp/rules",
            "test_message": "/api/notifications/whatsapp/test",
            "provider_status": "/api/v2/provider-platform/status",
        },
        "frontend_contract": {
            "render_as": "whatsapp_template_readiness",
            "show_missing_templates": True,
            "show_approval_badges": True,
            "show_24h_window_warning": True,
            "allow_template_creation": bool(integration_access.get("enabled")),
            "primary_locked_reason": integration_access.get("lock_reason_code"),
        },
    }


def _webview_blueprint_payload(
    tenant: TenantProfile,
    *,
    checkout_experience: Mapping[str, Any],
    integration_access: Mapping[str, Any],
) -> dict[str, Any]:
    slug = tenant.slug
    endpoints = checkout_experience.get("endpoints") if isinstance(checkout_experience.get("endpoints"), Mapping) else {}
    return {
        "enabled": bool(integration_access.get("enabled")),
        "respect_access_lock": True,
        "entrypoints": ["whatsapp", "widget", "web"],
        "checkout": {
            "mode": "conversation_guided_secure_webview",
            "active_entrypoint": checkout_experience.get("active_entrypoint"),
            "ready": bool(checkout_experience.get("ready")),
            "public_checkout_session": endpoints.get("public_checkout_session") or "/api/checkout/crear-preferencia",
            "public_widget_session": endpoints.get("public_widget_session") or "/api/public/widget-commerce-session",
            "payment_status": endpoints.get("admin_payment_status") or f"/api/v2/tenants/{slug}/payments/status",
            "confirmation_source": "server_to_server_webhook",
            "card_data_in_chat": False,
            "client_return_trusted": False,
        },
        "tracking": {
            "claim": "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}",
            "order": "/api/public/tracking/experience?kind=order&code={code}",
            "timeline_fallback": "timeline_only",
        },
        "catalog": {
            "public_catalog": f"/api/public/tenants/{slug}/catalog",
            "admin_catalog": f"/api/admin/tenants/{slug}/catalog/items",
        },
        "surveys": {
            "admin": "/api/v2/surveys",
            "public_template": "/e/{survey_slug}",
        },
        "security": {
            "requires_full_plan": True,
            "signed_session_required": True,
            "requires_tenant_authorization": True,
            "card_data_in_chat_allowed": False,
            "server_to_server_confirmation": True,
            "client_return_trusted": False,
        },
        "frontend_contract": {
            "render_as": "webview_checkout_and_tracking_hub",
            "show_security_copy": True,
            "show_ready_state": True,
            "show_upgrade_cta": not bool(integration_access.get("enabled")),
            "primary_locked_reason": integration_access.get("lock_reason_code"),
        },
    }


def _message_ux_policy_payload(
    *,
    channel_ready: bool,
    integration_access: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "enabled": bool(channel_ready),
        "rendering": {
            "plain_text_default": True,
            "interactive_when_approved": True,
            "never_hide_menu_numbers": True,
            "include_menu_and_cancel": True,
            "avoid_repeating_limit_message_on_navigation": True,
        },
        "interactive_limits": {
            "reply_buttons_max": 3,
            "list_rows_max": 10,
            "button_label_max_chars": 20,
            "list_button_max_chars": 20,
            "list_section_title_max_chars": 24,
            "row_title_max_chars": 24,
            "row_id_max_chars": 200,
            "description_max_chars": 72,
        },
        "languages": ["es", "en", "pt"],
        "accessibility": {
            "audio_notes": "transcribe_then_reason",
            "audio_reply_cache": "reuse_tts_cache_when_available",
            "screen_reader_text": "render_audio_text",
            "translation": "audio_translation_capabilities",
        },
        "gating": {
            "access_enabled": bool(integration_access.get("enabled")),
            "locked_reason": integration_access.get("lock_reason_code"),
            "requires_authenticated_admin": True,
            "demo_tenants_blocked": True,
        },
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
    template_blueprint = _template_blueprint_payload(
        tenant,
        channel_ready=channel_ready,
        integration_access=integration_access,
    )
    webview_blueprint = _webview_blueprint_payload(
        tenant,
        checkout_experience=checkout_experience,
        integration_access=integration_access,
    )
    message_ux_policy = _message_ux_policy_payload(
        channel_ready=channel_ready,
        integration_access=integration_access,
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
        "template_blueprint": template_blueprint,
        "webview_blueprint": webview_blueprint,
        "message_ux_policy": message_ux_policy,
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
                "template_blueprint",
                "webview_checkout",
                "message_ux_policy",
                "voice_realtime",
                "huggingface_ai",
                "enterprise_rules",
            ],
            "empty_state_behavior": "show_setup_checklist_and_safe_degradation",
        },
    }
