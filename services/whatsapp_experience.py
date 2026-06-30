from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
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
from services.tts_orchestrator import get_tts_cache_metrics


WHATSAPP_EXPERIENCE_CONTRACT_VERSION = "whatsapp.experience.v1"

APPROVED_TEMPLATE_STATUSES = {"approved", "active", "ready", "published", "online"}
PENDING_TEMPLATE_STATUSES = {"draft", "pending", "submitted", "in_review", "review", "twilio_review"}
REJECTED_TEMPLATE_STATUSES = {"rejected", "failed", "disabled", "paused"}
LOCAL_TWILIO_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "scripts" / "twilio_content_templates.local.json"
_FALSEY_CONFIG_VALUES = {"0", "false", "no", "off"}


def _runtime_flag_enabled(
    name: str,
    app_config: Mapping[str, Any] | None,
    *,
    default: bool = True,
) -> bool:
    value: Any = None
    if app_config is not None:
        value = app_config.get(name)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSEY_CONFIG_VALUES


def _tts_audio_cache_observability_payload(app_config: Mapping[str, Any] | None) -> dict[str, Any]:
    metrics = get_tts_cache_metrics()
    requests = int(metrics.get("requests", 0) or 0)
    cache_hits = int(metrics.get("cache_hits", 0) or 0)
    cache_misses = int(metrics.get("cache_misses", 0) or 0)
    failures = (
        int(metrics.get("provider_failures", 0) or 0)
        + int(metrics.get("generation_failures", 0) or 0)
        + int(metrics.get("warmup_failures", 0) or 0)
        + int(metrics.get("cache_write_failures", 0) or 0)
    )
    cache_enabled = _runtime_flag_enabled("TTS_CACHE_ENABLED", app_config, default=True)
    menu_audio_enabled = _runtime_flag_enabled("WHATSAPP_MENU_AUDIO_ENABLED", app_config, default=True)
    enabled = cache_enabled and menu_audio_enabled
    if not enabled:
        status = "disabled"
    elif failures:
        status = "degraded"
    elif requests:
        status = "active"
    else:
        status = "ready"

    return {
        "contract_version": "tts.audio_cache_observability.v1",
        "enabled": enabled,
        "ready": enabled,
        "cache_enabled": cache_enabled,
        "menu_audio_enabled": menu_audio_enabled,
        "status": status,
        "cache": "tts_audio_cache",
        "storage": {
            "public_path": "/static/audio_cache",
            "file_format": "mp3",
            "content_text_exposed": False,
        },
        "scope": ["main_menu", "claim_categories", "survey_menu", "catalog_menu", "status_menu"],
        "metrics": metrics,
        "summary": {
            "requests": requests,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "hit_rate": round(cache_hits / requests, 4) if requests else 0.0,
            "failures": failures,
            "warmup_successes": int(metrics.get("warmup_successes", 0) or 0),
        },
        "warmup": {
            "supported": True,
            "recommended_for": ["fixed_whatsapp_menus", "widget_quick_menus"],
            "fixed_menu_text_only": True,
            "sensitive_user_content_allowed": False,
        },
    }


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
    "gov_survey_invite": "chatboc_gov_survey_invite_v2",
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
    "finance_account_onboarding": "chatboc_finance_account_onboarding_v1",
    "finance_kyc_review": "chatboc_finance_kyc_review_v1",
    "finance_credit_offer": "chatboc_finance_credit_offer_v1",
    "finance_collection_due": "chatboc_finance_collection_due_v1",
    "finance_secure_payment": "chatboc_finance_secure_payment_v1",
    "finance_document_signature": "chatboc_finance_document_signature_v1",
    "finance_support_case": "chatboc_finance_support_case_v1",
    "finance_account_status": "chatboc_finance_account_status_v1",
    "finance_remittance_transfer": "chatboc_finance_remittance_transfer_v1",
    "finance_insurance_claim": "chatboc_finance_insurance_claim_v1",
    "finance_fee_financing": "chatboc_finance_fee_financing_v1",
    "finance_tax_payment": "chatboc_finance_tax_payment_v1",
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
    "financial_services": {
        "label": "Servicios financieros transaccionales",
        "purpose": "Resolver onboarding, KYC, creditos, cobranzas, pagos, firma y soporte dentro de WhatsApp con webviews seguros.",
        "items": [
            ("finance_account_onboarding", "onboarding", "webview", ["start_account_opening"]),
            ("finance_kyc_review", "identity", "webview", ["continue_kyc"]),
            ("finance_credit_offer", "credit", "webview", ["review_credit_offer"]),
            ("finance_collection_due", "collection", "webview", ["pay_debt", "request_payment_plan"]),
            ("finance_secure_payment", "payment", "webview", ["pay_securely"]),
            ("finance_document_signature", "signature", "webview", ["sign_document"]),
            ("finance_support_case", "support", "whatsapp", ["track_case", "human_handoff"]),
            ("finance_account_status", "banking", "webview", ["open_statement", "track_case"]),
            ("finance_remittance_transfer", "remittance", "webview", ["track_transfer", "download_transfer_receipt"]),
            ("finance_insurance_claim", "insurance", "webview", ["start_insurance_claim", "track_case"]),
            ("finance_fee_financing", "financing", "webview", ["review_financing_plan", "sign_document"]),
            ("finance_tax_payment", "payment", "webview", ["pay_tax", "download_receipt"]),
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
            ("finance_account_onboarding", "finance", "webview", ["start_account_opening"]),
            ("finance_secure_payment", "finance", "webview", ["pay_securely"]),
            ("finance_document_signature", "finance", "webview", ["sign_document"]),
            ("finance_remittance_transfer", "finance", "webview", ["track_transfer", "download_transfer_receipt"]),
            ("finance_insurance_claim", "finance", "webview", ["start_insurance_claim", "track_case"]),
        ],
    },
}


OPERATIONAL_TEMPLATE_CONTRACTS = {
    "welcome_menu": {
        "variables": ["contact_name", "tenant_name", "main_capabilities"],
        "fallback_body": "Hola, soy Chatboc. Elegi una opcion del menu o escribi tu consulta.",
        "qa_cases": ["chatboc_demo_menu", "colegio_menu_sandbox", "junin_texto_reclamo"],
        "audio_cache_menu_id": "main-menu",
    },
    "gov_claim_created": {
        "variables": ["claim_code", "category", "tracking_url"],
        "fallback_body": "Tu reclamo fue registrado. Te compartimos el codigo, categoria y link de seguimiento.",
        "qa_cases": ["junin_texto_reclamo", "junin_confirmacion_reclamo"],
        "receipt_contract": {
            "kind": "municipal_claim_receipt",
            "builder": "services.whatsapp_receipts.build_claim_created_template_pre_message",
            "pre_message_key": "_twilio_pre_messages",
            "tracking_required": True,
        },
    },
    "gov_claim_sla": {
        "variables": ["claim_code", "category", "tracking_url"],
        "fallback_body": "Reclamo listo para confirmar. Responde confirmar, editar o cancelar.",
        "qa_cases": ["junin_confirmacion_reclamo", "junin_ubicacion_confirmar"],
        "audio_cache_menu_id": "claim-categories",
    },
    "gov_claim_status_update": {
        "variables": ["claim_code", "status", "tracking_url"],
        "fallback_body": "Actualizamos el estado de tu reclamo y dejamos el seguimiento disponible.",
        "qa_cases": ["junin_dni_reclamo", "junin_confirmacion_reclamo"],
    },
    "order_checkout": {
        "variables": ["order_code", "total", "checkout_url"],
        "fallback_body": "Tu pedido esta listo. Usa el link seguro de checkout para revisar y pagar.",
        "qa_cases": ["cuatro_fincas_confirmar", "chatboc_demo_order_confirm"],
        "receipt_contract": {
            "kind": "pyme_order_checkout",
            "builder": "services.whatsapp_receipts.render_order_whatsapp",
            "pre_message_key": "_twilio_pre_messages",
            "tracking_required": False,
        },
    },
    "pyme_order_ready": {
        "variables": ["order_code", "total", "checkout_url"],
        "fallback_body": "Tu pedido esta preparado. Te enviamos total y link seguro para continuarlo.",
        "qa_cases": ["cuatro_fincas_pedido", "chatboc_demo_order_start"],
        "receipt_contract": {
            "kind": "pyme_order_receipt",
            "builder": "services.whatsapp_receipts.render_order_whatsapp",
            "pre_message_key": "_twilio_pre_messages",
            "tracking_required": False,
        },
    },
    "pyme_payment_link": {
        "variables": ["contact_name", "amount", "payment_url"],
        "fallback_body": "Tu link de pago esta disponible. Usalo solo desde el checkout seguro.",
        "qa_cases": ["chatboc_demo_order_confirm"],
    },
    "pyme_catalog_invite": {
        "variables": ["tenant_name", "catalog_url"],
        "fallback_body": "Te compartimos el catalogo actualizado para elegir productos desde una pantalla segura.",
        "qa_cases": ["cuatro_fincas_pedido", "chatboc_demo_empresas"],
        "audio_cache_menu_id": "catalog-menu",
    },
    "school_payment_due": {
        "variables": ["family_name", "concept", "amount", "payment_url"],
        "fallback_body": "Hay un concepto pendiente para revisar y pagar desde el portal seguro.",
        "qa_cases": ["colegio_menu_sandbox"],
    },
    "school_receipt_ready": {
        "variables": ["receipt_code", "student_name", "receipt_url"],
        "fallback_body": "El comprobante escolar esta disponible para descargar desde el portal.",
        "qa_cases": ["colegio_menu_sandbox", "colegio_detalle_audio_ubicacion"],
        "receipt_contract": {
            "kind": "school_payment_receipt",
            "builder": "education_contracts.receipt_ready",
            "pre_message_key": "_twilio_pre_messages",
            "tracking_required": False,
        },
    },
    "school_family_case_created": {
        "variables": ["case_code", "student_name", "status"],
        "fallback_body": "Creamos el caso familiar y dejamos el estado visible para seguimiento.",
        "qa_cases": ["colegio_seleccion_inasistencia", "colegio_detalle_audio_ubicacion"],
    },
    "finance_account_onboarding": {
        "variables": ["contact_name", "institution_name", "onboarding_url"],
        "fallback_body": "Podemos iniciar tu alta digital desde una pantalla segura con validacion de identidad.",
        "qa_cases": ["finance_account_opening", "chatboc_demo_finance_onboarding"],
    },
    "finance_kyc_review": {
        "variables": ["contact_name", "case_code", "kyc_url"],
        "fallback_body": "Tu validacion de identidad sigue pendiente. Continua desde el enlace seguro.",
        "qa_cases": ["finance_kyc_document_review"],
    },
    "finance_credit_offer": {
        "variables": ["contact_name", "offer_code", "offer_url"],
        "fallback_body": "Tenes una propuesta disponible para revisar condiciones antes de aceptarla.",
        "qa_cases": ["finance_credit_offer_review"],
    },
    "finance_collection_due": {
        "variables": ["contact_name", "concept", "payment_url"],
        "fallback_body": "Tenes un saldo pendiente. Podes pagarlo o solicitar un plan desde el portal seguro.",
        "qa_cases": ["finance_collection_payment_plan"],
    },
    "finance_secure_payment": {
        "variables": ["operation_code", "amount", "payment_url"],
        "fallback_body": "La operacion esta lista para pagar desde un checkout seguro. Nunca pedimos datos de tarjeta por chat.",
        "qa_cases": ["finance_secure_payment"],
        "receipt_contract": {
            "kind": "finance_payment_receipt",
            "builder": "payments_contracts.finance_payment_receipt",
            "pre_message_key": "_twilio_pre_messages",
            "tracking_required": True,
        },
    },
    "finance_document_signature": {
        "variables": ["document_code", "document_type", "signature_url"],
        "fallback_body": "El documento esta listo para firmar digitalmente desde una pantalla segura.",
        "qa_cases": ["finance_document_signature"],
    },
    "finance_support_case": {
        "variables": ["case_code", "status", "support_url"],
        "fallback_body": "Creamos tu caso de soporte financiero y dejamos el seguimiento disponible.",
        "qa_cases": ["finance_support_handoff"],
    },
    "finance_account_status": {
        "variables": ["contact_name", "account_code", "statement_url"],
        "fallback_body": "Tu resumen esta disponible en una pantalla segura. No compartimos saldos ni datos sensibles por chat.",
        "qa_cases": ["finance_account_status"],
    },
    "finance_remittance_transfer": {
        "variables": ["operation_code", "beneficiary_name", "tracking_url"],
        "fallback_body": "Tu transferencia o remesa quedo registrada. Podes seguir el estado desde el enlace seguro.",
        "qa_cases": ["finance_remittance_transfer"],
        "receipt_contract": {
            "kind": "finance_transfer_receipt",
            "builder": "payments_contracts.finance_transfer_receipt",
            "pre_message_key": "_twilio_pre_messages",
            "tracking_required": True,
        },
    },
    "finance_insurance_claim": {
        "variables": ["claim_code", "claim_type", "tracking_url"],
        "fallback_body": "Registramos tu siniestro o reclamo de seguro. El seguimiento queda disponible para adjuntar documentacion.",
        "qa_cases": ["finance_insurance_claim"],
    },
    "finance_fee_financing": {
        "variables": ["contact_name", "concept", "financing_url"],
        "fallback_body": "Podemos simular y solicitar un plan de financiacion desde una pantalla segura antes de confirmar.",
        "qa_cases": ["finance_fee_financing"],
    },
    "finance_tax_payment": {
        "variables": ["concept", "due_date", "payment_url"],
        "fallback_body": "La tasa o concepto esta listo para revisar y pagar desde checkout seguro.",
        "qa_cases": ["finance_tax_payment"],
    },
}


def _action_contracts(actions: list[str], *, entrypoint: str) -> list[dict[str, Any]]:
    webview_actions = {
        "add_to_cart",
        "checkout",
        "download_certificate",
        "download_document",
        "download_receipt",
        "continue_kyc",
        "open_catalog",
        "open_event",
        "open_help_center",
        "open_support_case",
        "pay_debt",
        "pay_expense",
        "pay_fee",
        "pay_order",
        "pay_securely",
        "pay_tax",
        "request_payment_plan",
        "review_credit_offer",
        "review_financing_plan",
        "review_order",
        "review_quote",
        "sign_document",
        "start_account_opening",
        "start_admission",
        "start_insurance_claim",
        "start_procedure",
        "track_claim",
        "track_case",
        "track_order",
        "track_transfer",
        "vote",
    }
    results: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action or "").strip()
        if not action_id:
            continue
        surface = "signed_webview" if entrypoint == "webview" or action_id in webview_actions else "whatsapp_reply"
        results.append(
            {
                "id": action_id,
                "surface": surface,
                "requires_approved_template": surface == "whatsapp_reply",
                "requires_signed_url": surface == "signed_webview",
            }
        )
    return results


def _audio_cache_contract_for_template(
    template_id: str,
    *,
    stage: str,
    entrypoint: str,
    contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    menu_id = contract.get("audio_cache_menu_id")
    if not menu_id and stage != "menu":
        return None
    menu_id = str(menu_id or template_id).replace("_", "-")
    return {
        "enabled": True,
        "kind": "fixed_menu",
        "scope": "tenant",
        "inclusive": True,
        "menu_id": menu_id,
        "entrypoint": entrypoint,
        "cache": "tts_audio_cache",
        "namespace_pattern": "whatsapp:menu:{tenant_slug}:{menu_id}:whatsapp:{variant}:v{version}",
        "invalidate_on": ["menu_version_change", "tenant_voice_profile_change", "language_change"],
    }


def _operational_template_contract(
    template_id: str,
    *,
    stage: str,
    entrypoint: str,
    actions: list[str],
) -> dict[str, Any]:
    contract = OPERATIONAL_TEMPLATE_CONTRACTS.get(_lower(template_id), {})
    variables = [str(value) for value in contract.get("variables", []) if str(value).strip()]
    next_actions = contract.get("next_actions")
    if not isinstance(next_actions, list):
        next_actions = _action_contracts(actions, entrypoint=entrypoint)
    fallback_body = str(
        contract.get("fallback_body")
        or f"Actualizacion de {template_id}. Continuamos por mensaje de texto mientras la plantilla se aprueba."
    )
    payload: dict[str, Any] = {
        "contract_version": "whatsapp.operational_template_contract.v1",
        "variables": variables,
        "variable_contract": {
            "format": "twilio_content_variables",
            "required": variables,
            "sample_values": _numbered_content_variables(variables),
        },
        "fallback": {
            "mode": "plain_text",
            "trigger": "missing_pending_rejected_or_unapproved_template",
            "body": fallback_body,
            "inside_24h_allowed": True,
            "outside_24h_requires_approved_template": True,
        },
        "next_actions": next_actions,
        "qa_cases": [str(value) for value in contract.get("qa_cases", []) if str(value).strip()],
    }
    receipt_contract = contract.get("receipt_contract")
    if isinstance(receipt_contract, Mapping):
        payload["receipt_contract"] = dict(receipt_contract)
    audio_cache = _audio_cache_contract_for_template(
        template_id,
        stage=stage,
        entrypoint=entrypoint,
        contract=contract,
    )
    if audio_cache:
        payload["audio_cache_contract"] = audio_cache
    return payload


def _template_execution_hint(
    *,
    template_id: str,
    stage: str,
    entrypoint: str,
    actions: list[str],
    components: list[str] | None = None,
) -> dict[str, Any]:
    components = components or []
    requires_webview = entrypoint == "webview" or "cta_webview" in components
    if requires_webview or "cta_url" in components:
        twilio_type = "twilio/call-to-action"
    elif len(actions) >= 4:
        twilio_type = "twilio/list-picker"
    elif actions:
        twilio_type = "twilio/quick-reply"
    else:
        twilio_type = "twilio/text"

    flow_candidate = stage in {
        "admission",
        "case",
        "claim",
        "collection",
        "checkout",
        "credit",
        "education",
        "finance",
        "identity",
        "onboarding",
        "payment",
        "procedure",
        "signature",
        "support",
        "survey",
        "banking",
        "remittance",
        "insurance",
        "financing",
    }
    webview_role = None
    if requires_webview:
        if stage in {"payment", "checkout", "collection"}:
            webview_role = "secure_checkout"
        elif stage in {"catalog", "order", "quote"}:
            webview_role = "catalog_cart_order"
        elif stage in {"claim", "case", "procedure", "support", "onboarding", "identity", "credit", "finance", "signature"}:
            webview_role = "case_tracking_or_form"
        elif stage in {"banking", "remittance", "insurance", "financing"}:
            webview_role = "secure_transactional_form"
        elif stage in {"survey", "announcement", "event"}:
            webview_role = "survey_or_content_detail"
        else:
            webview_role = "signed_context_screen"

    return {
        "twilio_type": twilio_type,
        "button_strategy": (
            "cta_webview"
            if requires_webview
            else "list_picker"
            if twilio_type == "twilio/list-picker"
            else "quick_reply"
            if twilio_type == "twilio/quick-reply"
            else "text_only"
        ),
        "meta_surface": {
            "whatsapp_template": True,
            "whatsapp_flows_candidate": flow_candidate,
            "commerce_catalog_candidate": stage in {"catalog", "order", "checkout"},
            "payments_native_candidate": stage in {"payment", "checkout"},
        },
        "webview": {
            "required": bool(requires_webview),
            "role": webview_role,
            "must_use_signed_context": bool(requires_webview),
            "must_confirm_by_webhook": stage in {"payment", "checkout"},
            "keep_sensitive_data_out_of_chat": stage in {"payment", "checkout", "admission"},
        },
        "automation": {
            "create_with_twilio_content_api": True,
            "submit_for_meta_approval": True,
            "register_content_sid_in_template_registry": True,
            "fallback_to_text_until_approved": True,
            "template_key": CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(template_id)) or template_id,
        },
    }


def _stage_for_blueprint_item(item: Mapping[str, Any]) -> str:
    raw = f"{item.get('stage') or ''} {item.get('id') or ''} {item.get('name') or ''} {item.get('purpose') or ''}".lower()
    if any(token in raw for token in ("payment", "pago", "cuota", "tasa", "checkout")):
        return "payment"
    if any(token in raw for token in ("order", "pedido", "quote", "cotizacion", "catalog")):
        return "order"
    if any(token in raw for token in ("claim", "reclamo", "case", "caso", "support", "soporte")):
        return "claim"
    if any(token in raw for token in ("survey", "encuesta", "votar", "votacion")):
        return "survey"
    if any(token in raw for token in ("turn", "turno", "appointment")):
        return "appointment"
    if any(token in raw for token in ("admission", "admision", "procedure", "tramite")):
        return "procedure"
    if any(token in raw for token in ("certificate", "certificado", "document")):
        return "document"
    return str(item.get("stage") or item.get("id") or "").split("_")[0]


def _entrypoint_for_blueprint_item(item: Mapping[str, Any]) -> str:
    components = item.get("components") if isinstance(item.get("components"), list) else []
    if "cta_webview" in components or "cta_url" in components:
        return "webview"
    return "whatsapp"


def _enrich_blueprint_item(item: dict[str, Any], *, entrypoint: str | None = None) -> dict[str, Any]:
    components = item.get("components") if isinstance(item.get("components"), list) else []
    actions = item.get("suggested_actions") if isinstance(item.get("suggested_actions"), list) else []
    stage = _stage_for_blueprint_item(item)
    resolved_entrypoint = entrypoint or _entrypoint_for_blueprint_item(item)
    item["execution"] = _template_execution_hint(
        template_id=str(item.get("id") or item.get("name") or ""),
        stage=stage,
        entrypoint=resolved_entrypoint,
        actions=[str(action) for action in actions],
        components=[str(component) for component in components],
    )
    return item


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None


@lru_cache(maxsize=1)
def _local_twilio_manifest_template_map() -> dict[str, dict[str, Any]]:
    """Best-effort local fallback that mirrors the sender resolver manifest."""
    try:
        raw = json.loads(LOCAL_TWILIO_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}

    templates = raw.get("templates") if isinstance(raw, dict) else None
    if not isinstance(templates, dict):
        return {}

    payload: dict[str, dict[str, Any]] = {}
    for name, entry in templates.items():
        if not isinstance(entry, dict):
            continue
        status = _lower(entry.get("approvalStatus") or entry.get("approval_status"))
        if not status:
            status = "approved" if entry.get("approved") is True else "draft"
        payload[_lower(name)] = {
            "source": "local_twilio_manifest",
            "id": None,
            "name": name,
            "language": str(entry.get("language") or "es"),
            "category": entry.get("category"),
            "status": status,
            "content_sid": entry.get("sid") or entry.get("content_sid"),
            "external_template_id": entry.get("external_template_id"),
            "body_preview": None,
            "components": [],
            "metadata": {"manifest_version": raw.get("version")},
            "last_sync_at": entry.get("lastStatusAt") or entry.get("updatedAt"),
        }
    return payload


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


def _conversation_intelligence_payload(
    tenant: TenantProfile,
    cfg: Mapping[str, Any],
    app_config: Mapping[str, Any] | None,
    *,
    audio_cache: Mapping[str, Any],
) -> dict[str, Any]:
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
            "iniciar_alta_digital",
            "continuar_kyc",
            "evaluar_oferta_credito",
            "gestionar_cobranza",
            "firmar_documento",
            "registrar_encuesta",
            "derivar_humano",
        ],
        "audio_cache": dict(audio_cache),
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

    for name, template in _local_twilio_manifest_template_map().items():
        templates[name] = template

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
    exact_matches: list[tuple[str, dict[str, Any]]] = []
    for candidate in _template_lookup_candidates(template_id):
        template = templates.get(candidate)
        if template:
            exact_matches.append((candidate, template))

    prefix = f"chatboc_{_lower(template_id)}_v"
    matches = [
        (name, template)
        for name, template in templates.items()
        if name.startswith(prefix)
    ]
    matches = exact_matches + [
        (name, template)
        for name, template in matches
        if all(existing_name != name for existing_name, _ in exact_matches)
    ]
    if not matches:
        return None, None
    approved = [
        (name, template)
        for name, template in matches
        if _lower(template.get("status")) in APPROVED_TEMPLATE_STATUSES
    ]
    pending = [
        (name, template)
        for name, template in matches
        if _lower(template.get("status")) in PENDING_TEMPLATE_STATUSES
    ]
    return sorted(approved or pending or matches, key=lambda item: item[0], reverse=True)[0]


def _template_status(templates: Mapping[str, dict[str, Any]], template_id: str) -> dict[str, Any]:
    resolved_name, template = _pick_registered_template(templates, template_id)
    if not template:
        return {
            "configured": False,
            "approved": False,
            "pending": False,
            "rejected": False,
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
        "rejected": status in REJECTED_TEMPLATE_STATUSES,
        "status": status or "unknown",
        "source": template.get("source"),
        "resolved_name": resolved_name or template.get("name"),
        "expected_friendly_name": CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(template_id)),
        "content_sid": template.get("content_sid"),
        "external_template_id": template.get("external_template_id"),
        "last_sync_at": template.get("last_sync_at"),
    }


def _template_readiness_payload(
    status_payload: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    webview = execution.get("webview") if isinstance(execution.get("webview"), Mapping) else {}
    meta_surface = execution.get("meta_surface") if isinstance(execution.get("meta_surface"), Mapping) else {}
    automation = execution.get("automation") if isinstance(execution.get("automation"), Mapping) else {}
    twilio_type = str(execution.get("twilio_type") or "twilio/text")

    if not status_payload.get("configured"):
        state = "missing"
        severity = "blocking"
        next_action = "create_template_with_twilio_content_api"
    elif status_payload.get("rejected"):
        state = "rejected"
        severity = "blocking"
        next_action = "revise_copy_category_or_variables_and_resubmit"
    elif status_payload.get("pending"):
        state = "pending_approval"
        severity = "warning"
        next_action = "refresh_twilio_status_or_wait_for_meta_approval"
    elif not status_payload.get("approved"):
        state = "not_approved"
        severity = "warning"
        next_action = "submit_template_for_meta_approval"
    elif webview.get("required"):
        state = "approved_requires_webview"
        severity = "ready_with_dependency"
        next_action = "verify_signed_webview_and_server_webhook"
    else:
        state = "ready"
        severity = "ready"
        next_action = "ready_to_send"

    return {
        "state": state,
        "severity": severity,
        "next_action": next_action,
        "production_send_allowed": bool(status_payload.get("approved")),
        "fallback_to_text": not bool(status_payload.get("approved")),
        "twilio_type": twilio_type,
        "content_sid": status_payload.get("content_sid"),
        "source": status_payload.get("source"),
        "requires_webview": bool(webview.get("required")),
        "requires_whatsapp_flow_design": bool(meta_surface.get("whatsapp_flows_candidate") and not webview.get("required")),
        "requires_catalog_sync": bool(meta_surface.get("commerce_catalog_candidate")),
        "automation_command": automation.get("template_key"),
    }


def _sample_value_for_variable(variable: str, *, tenant: TenantProfile | None = None) -> str:
    key = _lower(variable)
    tenant_name = getattr(tenant, "nombre", None) or getattr(tenant, "name", None) or "Chatboc"
    samples = {
        "contact_name": "Marcelo",
        "tenant_name": str(tenant_name),
        "main_capabilities": "reclamos, pedidos y atencion",
        "case_code": "M-123456",
        "case_or_order_code": "M-123456",
        "status": "Recibido",
        "tracking_url": "https://www.chatboc.ar/tracking/claim/123456?pin=900144",
        "order_code": "P-123456",
        "total": "$ 12.500",
        "checkout_url": "https://www.chatboc.ar/checkout/123456",
        "survey_title": "Encuesta de satisfaccion",
        "survey_url": "https://www.chatboc.ar/e/encuesta-demo",
        "payment_url": "https://www.chatboc.ar/checkout/123456",
        "amount": "$ 12.500",
        "quote_code": "COT-1234",
        "quote_url": "https://www.chatboc.ar/cotizacion/1234",
        "catalog_url": "https://www.chatboc.ar/catalogo/demo",
        "family_name": "Familia Perez",
        "concept": "cuota mensual",
        "receipt_code": "REC-1234",
        "student_name": "Juan Perez",
        "receipt_url": "https://www.chatboc.ar/comprobantes/1234",
        "event_title": "Reunion de familias",
        "event_date": "15/07 18:00",
        "event_url": "https://www.chatboc.ar/eventos/1234",
        "claim_code": "M-123456",
        "category": "Arreglo de calle",
        "turn_code": "T-1234",
        "office": "Mesa de entradas",
        "date_time": "15/07 09:30",
        "turn_url": "https://www.chatboc.ar/turnos/1234",
        "document_code": "DOC-1234",
        "document_url": "https://www.chatboc.ar/documentos/1234",
    }
    return samples.get(key, f"valor_{key or 'demo'}")


def _numbered_content_variables(variables: list[str], *, tenant: TenantProfile | None = None) -> dict[str, str]:
    return {
        str(index): _sample_value_for_variable(variable, tenant=tenant)
        for index, variable in enumerate(variables, start=1)
    }


URL_TEMPLATE_VARIABLES = {
    "url",
    "link",
    "tracking_url",
    "checkout_url",
    "payment_url",
    "survey_url",
    "catalog_url",
    "receipt_url",
    "turn_url",
    "document_url",
    "quote_url",
    "event_url",
    "onboarding_url",
    "kyc_url",
    "offer_url",
    "signature_url",
    "support_url",
    "statement_url",
    "financing_url",
}


def _public_web_base_url() -> str:
    base = (
        os.getenv("APP_BASE_URL")
        or os.getenv("PUBLIC_FRONTEND_URL")
        or os.getenv("FRONTEND_URL")
        or os.getenv("CHATBOC_PUBLIC_BASE_URL")
        or "https://www.chatboc.ar"
    )
    base = str(base).strip().rstrip("/") or "https://www.chatboc.ar"
    if not base.startswith(("http://", "https://")):
        return f"https://{base.lstrip('/')}"
    return base


def _cta_url_variable_index(variables: list[str]) -> int | None:
    for index, variable in enumerate(variables, start=1):
        if _lower(variable) in URL_TEMPLATE_VARIABLES:
            return index
    return None


def _cta_route_suffix_sample(
    variable: str,
    *,
    item: Mapping[str, Any],
    tenant: TenantProfile | None = None,
) -> str:
    key = _lower(variable)
    stage = _stage_for_blueprint_item(item)
    template_id = _lower(item.get("id") or item.get("name"))
    tenant_slug = str(getattr(tenant, "slug", None) or "demo").strip() or "demo"
    session = "session-demo-123456"

    if template_id.startswith("finance_") and key in {"checkout_url", "payment_url"}:
        return f"finanzas/{tenant_slug}/operacion/OP-1001?session={session}"
    if key in {"checkout_url", "payment_url"}:
        return f"t/{tenant_slug}/checkout"
    if key == "catalog_url":
        return f"t/{tenant_slug}/market"
    if key == "survey_url":
        return "e/encuesta-demo"
    if key in {"quote_url"}:
        return f"t/{tenant_slug}/checkout?quote=COT-1234"
    if key in {"receipt_url"}:
        return f"t/{tenant_slug}/portal/pedidos?receipt=REC-1234"
    if key in {"turn_url"}:
        return f"t/{tenant_slug}/portal/dashboard?turno=T-1234"
    if key in {"event_url"}:
        return f"t/{tenant_slug}/portal/eventos?event=EVT-1234"
    if key in {"document_url"}:
        return f"t/{tenant_slug}/portal/cuenta?document=DOC-1234"
    if key in {"onboarding_url", "kyc_url"}:
        return f"finanzas/{tenant_slug}/alta/ONB-1001?session={session}"
    if key in {"offer_url", "signature_url", "financing_url"}:
        return f"finanzas/{tenant_slug}/operacion/OP-1001?session={session}"
    if key in {"statement_url"}:
        return f"finanzas/{tenant_slug}/cuentas/CTA-1001?session={session}"
    if key in {"support_url"}:
        return f"finanzas/{tenant_slug}/cuentas/FIN-1001?session={session}"
    if key in {"tracking_url", "url", "link"}:
        if stage in {"order", "delivery", "tracking", "checkout", "catalog"} or template_id.startswith("pyme_"):
            return f"tracking/order/P-123456?tenant_slug={tenant_slug}"
        if template_id.startswith("finance_insurance"):
            return f"finanzas/{tenant_slug}/seguros/SIN-1001?session={session}"
        if template_id.startswith("finance_remittance"):
            return f"finanzas/{tenant_slug}/transferencias/TRF-1001?session={session}"
        return f"tracking/claim/123456?pin=900144&tenant_slug={tenant_slug}"
    return f"t/{tenant_slug}/portal/dashboard"


def _numbered_content_variables_for_creation(
    variables: list[str],
    *,
    item: Mapping[str, Any],
    tenant: TenantProfile | None = None,
    cta_url_index: int | None = None,
) -> dict[str, str]:
    values = _numbered_content_variables(variables, tenant=tenant)
    if cta_url_index is not None and 1 <= cta_url_index <= len(variables):
        values[str(cta_url_index)] = _cta_route_suffix_sample(
            variables[cta_url_index - 1],
            item=item,
            tenant=tenant,
        )
    return values


def _body_with_canonical_cta_url(body: str, cta_url_index: int | None) -> str:
    if cta_url_index is None:
        return body
    placeholder = "{{" + str(cta_url_index) + "}}"
    if placeholder not in body:
        return body
    return body.replace(placeholder, f"{_public_web_base_url()}/{placeholder}")


def _template_button_title(item: Mapping[str, Any]) -> str:
    stage = _stage_for_blueprint_item(item)
    if stage in {"payment", "checkout"}:
        return "Pagar seguro"
    if stage in {"order", "catalog"}:
        return "Abrir catalogo"
    if stage in {"survey"}:
        return "Participar"
    if stage in {"claim", "case", "procedure", "support"}:
        return "Ver seguimiento"
    if stage in {"document"}:
        return "Ver documento"
    return "Abrir"


def _template_content_capabilities(
    *,
    twilio_type: str,
    types: Mapping[str, Any],
    variables: list[str],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    cta_type = types.get("twilio/call-to-action") if isinstance(types.get("twilio/call-to-action"), Mapping) else {}
    quick_reply_type = types.get("twilio/quick-reply") if isinstance(types.get("twilio/quick-reply"), Mapping) else {}
    list_type = types.get("twilio/list-picker") if isinstance(types.get("twilio/list-picker"), Mapping) else {}
    cta_actions = cta_type.get("actions") if isinstance(cta_type.get("actions"), list) else []
    quick_reply_actions = quick_reply_type.get("actions") if isinstance(quick_reply_type.get("actions"), list) else []
    list_items = list_type.get("items") if isinstance(list_type.get("items"), list) else []
    stage = _stage_for_blueprint_item(item)
    variable_names = {str(value).lower() for value in variables}
    has_url_variable = any(
        token in variable_names
        for token in {"url", "link", "tracking_url", "checkout_url", "payment_url", "survey_url", "catalog_url"}
    )
    has_url_cta = any(str(action.get("type") or "").upper() == "URL" for action in cta_actions if isinstance(action, Mapping))
    content_family = "text"
    if twilio_type == "twilio/call-to-action":
        content_family = "cta_webview"
    elif twilio_type == "twilio/quick-reply":
        content_family = "quick_decision"
    elif twilio_type == "twilio/list-picker":
        content_family = "menu_picker"

    return {
        "content_family": content_family,
        "stage": stage,
        "meta_supported_type": twilio_type,
        "cta_url_count": sum(1 for action in cta_actions if isinstance(action, Mapping) and str(action.get("type") or "").upper() == "URL"),
        "quick_reply_count": len(quick_reply_actions),
        "list_item_count": len(list_items),
        "webview_ready": bool(has_url_cta and (has_url_variable or stage in {"payment", "checkout", "order", "catalog", "survey", "claim", "case"})),
        "requires_signed_url": bool(has_url_cta),
        "keeps_user_in_conversation": twilio_type in {"twilio/quick-reply", "twilio/list-picker"},
    }


def _template_creation_manifest_item(
    item: Mapping[str, Any],
    *,
    tenant: TenantProfile | None = None,
) -> dict[str, Any]:
    execution = item.get("execution") if isinstance(item.get("execution"), Mapping) else {}
    status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
    readiness = item.get("readiness") if isinstance(item.get("readiness"), Mapping) else {}
    body = str(item.get("body") or "").strip()
    variables = [str(value) for value in item.get("variables", [])] if isinstance(item.get("variables"), list) else []
    friendly_name = str(item.get("friendly_name") or item.get("name") or item.get("id") or "").strip()
    language = str(item.get("language") or "es").strip() or "es"
    category = str(item.get("category") or "UTILITY").strip().upper() or "UTILITY"
    twilio_type = str(execution.get("twilio_type") or readiness.get("twilio_type") or "twilio/text")
    cta_url_index = _cta_url_variable_index(variables) if twilio_type == "twilio/call-to-action" else None
    sample_values = _numbered_content_variables_for_creation(
        variables,
        item=item,
        tenant=tenant,
        cta_url_index=cta_url_index,
    )
    base_body = body or f"Actualizacion de {friendly_name}: {{{{1}}}}"
    text_body = _body_with_canonical_cta_url(base_body, cta_url_index)
    types: dict[str, Any] = {"twilio/text": {"body": text_body}}

    if twilio_type == "twilio/quick-reply":
        action_labels = [str(action).replace("_", " ").title()[:20] for action in item.get("suggested_actions", []) if action]
        if not action_labels:
            action_labels = ["Menu", "Ayuda", "Cancelar"]
        types["twilio/quick-reply"] = {
            "body": body or types["twilio/text"]["body"],
            "actions": [
                {"title": label, "id": f"{_lower(item.get('id'))}:{index}"}
                for index, label in enumerate(action_labels[:3], start=1)
            ],
        }
    elif twilio_type == "twilio/list-picker":
        action_labels = [str(action).replace("_", " ").title()[:24] for action in item.get("suggested_actions", []) if action]
        if not action_labels:
            action_labels = ["Menu", "Estado", "Ayuda", "Cancelar"]
        types["twilio/list-picker"] = {
            "body": body or types["twilio/text"]["body"],
            "button": "Opciones",
            "items": [
                {"item": label, "id": f"{_lower(item.get('id'))}:{index}"}
                for index, label in enumerate(action_labels[:10], start=1)
            ],
        }
    elif twilio_type == "twilio/call-to-action":
        cta_url = f"{_public_web_base_url()}/{{{{{cta_url_index or max(1, len(variables))}}}}}"
        types["twilio/call-to-action"] = {
            "body": text_body,
            "actions": [
                {
                    "type": "URL",
                    "title": _template_button_title(item),
                    "url": cta_url,
                }
            ],
        }
    capabilities = _template_content_capabilities(
        twilio_type=twilio_type,
        types=types,
        variables=variables,
        item=item,
    )
    meta_surface = execution.get("meta_surface") if isinstance(execution.get("meta_surface"), Mapping) else {}
    meta_business = {
        "recommended_surface": (
            "whatsapp_flow"
            if bool(meta_surface.get("whatsapp_flows_candidate"))
            else "commerce_catalog"
            if bool(meta_surface.get("commerce_catalog_candidate"))
            else "approved_template"
        ),
        "whatsapp_flows_candidate": bool(meta_surface.get("whatsapp_flows_candidate")),
        "commerce_catalog_candidate": bool(meta_surface.get("commerce_catalog_candidate")),
        "payments_native_candidate": bool(meta_surface.get("payments_native_candidate")),
        "cta_webview_candidate": bool(capabilities["webview_ready"]),
        "approval_category": category,
        "outside_24h_requires_approval": True,
    }

    return {
        "id": item.get("id") or item.get("name"),
        "friendly_name": friendly_name,
        "language": language,
        "category": category,
        "meta_category": category,
        "twilio_type": twilio_type,
        "content_family": capabilities["content_family"],
        "action_capabilities": capabilities,
        "meta_business": meta_business,
        "variables": variables,
        "sample_values": sample_values,
        "create_request": {
            "friendly_name": friendly_name,
            "language": language,
            "variables": sample_values,
            "types": types,
        },
        "approval_request": {
            "name": friendly_name,
            "category": category,
        },
        "send_example": {
            "content_sid": status.get("content_sid") or "HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "content_variables": sample_values,
        },
        "readiness": {
            "state": readiness.get("state"),
            "severity": readiness.get("severity"),
            "next_action": readiness.get("next_action"),
            "configured": bool(status.get("configured")),
            "approved": bool(status.get("approved")),
        },
        "quality_gate": {
            "variables_sequential": True,
            "sample_values_required": True,
            "meta_approval_required_outside_24h": True,
            "contains_card_or_payment_data": False,
            "requires_signed_url_for_cta": bool(capabilities["requires_signed_url"]),
            "webview_ready": bool(capabilities["webview_ready"]),
            "needs_copy_review": not bool(body),
        },
    }


def _template_creation_manifest_payload(
    *,
    tenant: TenantProfile,
    required_templates: list[dict[str, Any]],
    vertical_templates: Mapping[str, list[dict[str, Any]]],
    operational_templates: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    template_items: list[Mapping[str, Any]] = []
    template_items.extend(required_templates)
    for vertical in ("gobierno", "colegio", "pyme"):
        template_items.extend(vertical_templates.get(vertical, []))
    template_items.extend(operational_templates or [])

    deduped_items: list[Mapping[str, Any]] = []
    seen_template_ids: set[str] = set()
    for item in template_items:
        template_key = _lower(item.get("id") or item.get("name") or item.get("friendly_name"))
        if not template_key or template_key in seen_template_ids:
            continue
        seen_template_ids.add(template_key)
        deduped_items.append(item)

    manifests = [
        _template_creation_manifest_item(item, tenant=tenant)
        for item in deduped_items
    ]
    actionable = [
        item
        for item in manifests
        if item["readiness"].get("severity") in {"blocking", "warning", "ready_with_dependency"}
    ]
    by_type: dict[str, int] = {}
    by_content_family: dict[str, int] = {}
    by_meta_surface: dict[str, int] = {}
    for item in manifests:
        by_type[str(item.get("twilio_type") or "twilio/text")] = by_type.get(str(item.get("twilio_type") or "twilio/text"), 0) + 1
        family = str(item.get("content_family") or "text")
        by_content_family[family] = by_content_family.get(family, 0) + 1
        meta_business = item.get("meta_business") if isinstance(item.get("meta_business"), Mapping) else {}
        surface = str(meta_business.get("recommended_surface") or "approved_template")
        by_meta_surface[surface] = by_meta_surface.get(surface, 0) + 1

    return {
        "contract_version": "twilio.content.creation_manifest.v1",
        "sdk": "client.content.v1.contents.create",
        "approval_sdk": "client.content.v1.contents(content_sid).approval_requests.create",
        "templates_total": len(manifests),
        "actionable_total": len(actionable),
        "by_twilio_type": by_type,
        "by_content_family": by_content_family,
        "by_meta_surface": by_meta_surface,
        "webview_ready_total": sum(
            1
            for item in manifests
            if bool((item.get("action_capabilities") or {}).get("webview_ready"))
        ),
        "meta_business_readiness": {
            "whatsapp_flows_candidates": sum(
                1
                for item in manifests
                if bool(((item.get("meta_business") or {}).get("whatsapp_flows_candidate")))
            ),
            "commerce_catalog_candidates": sum(
                1
                for item in manifests
                if bool(((item.get("meta_business") or {}).get("commerce_catalog_candidate")))
            ),
            "payments_native_candidates": sum(
                1
                for item in manifests
                if bool(((item.get("meta_business") or {}).get("payments_native_candidate")))
            ),
            "cta_webview_candidates": sum(
                1
                for item in manifests
                if bool(((item.get("meta_business") or {}).get("cta_webview_candidate")))
            ),
            "recommendation": "Usar templates aprobadas para recontacto, WhatsApp Flows donde Meta lo habilite y webviews firmados para pagos/datos sensibles.",
        },
        "items": actionable,
        "all_template_ids": [str(item.get("id") or "") for item in manifests if item.get("id")],
        "policy": {
            "create_first": True,
            "submit_for_meta_approval": True,
            "store_content_sid_in_message_template_registry": True,
            "do_not_send_unapproved_outside_24h_window": True,
        },
    }


def _attach_template_status_and_readiness(
    item: dict[str, Any],
    templates: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    status_payload = _template_status(templates, str(item.get("id") or item.get("name") or ""))
    execution = item.get("execution") if isinstance(item.get("execution"), Mapping) else {}
    item["status"] = status_payload
    item["readiness"] = _template_readiness_payload(status_payload, execution)
    return item


def _readiness_action_item(item: Mapping[str, Any], *, group: str) -> dict[str, Any]:
    readiness = item.get("readiness") if isinstance(item.get("readiness"), Mapping) else {}
    status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
    return {
        "id": item.get("id"),
        "friendly_name": item.get("friendly_name"),
        "group": group,
        "state": readiness.get("state"),
        "severity": readiness.get("severity"),
        "next_action": readiness.get("next_action"),
        "twilio_type": readiness.get("twilio_type"),
        "status": status.get("status"),
        "content_sid": status.get("content_sid"),
        "source": status.get("source"),
    }


def _sorted_readiness_actions(items: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    severity_order = {"blocking": 0, "warning": 1, "ready_with_dependency": 2, "ready": 3}
    actionable = [
        item
        for item in items
        if item.get("severity") in {"blocking", "warning", "ready_with_dependency"}
    ]
    return sorted(
        actionable,
        key=lambda item: (
            severity_order.get(str(item.get("severity")), 9),
            str(item.get("group") or ""),
            str(item.get("id") or ""),
        ),
    )[:limit]


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
    execution = _template_execution_hint(
        template_id=template_id,
        stage=stage,
        entrypoint=entrypoint,
        actions=actions,
    )
    template_contract = _operational_template_contract(
        template_id,
        stage=stage,
        entrypoint=entrypoint,
        actions=actions,
    )
    item = {
        "id": template_id,
        "friendly_name": friendly_name,
        "stage": stage,
        "entrypoint": entrypoint,
        "execution": execution,
        "requires_webview": entrypoint == "webview",
        "in_chat_action": entrypoint in {"whatsapp", "widget_and_whatsapp"},
        "widget_action": entrypoint in {"widget", "widget_and_whatsapp", "webview"},
        "actions": actions,
        "status": status_payload,
        "readiness": _template_readiness_payload(status_payload, execution),
        "variables": template_contract["variables"],
        "variable_contract": template_contract["variable_contract"],
        "fallback": template_contract["fallback"],
        "next_actions": template_contract["next_actions"],
        "qa_cases": template_contract["qa_cases"],
        "template_contract": template_contract,
    }
    if "receipt_contract" in template_contract:
        item["receipt_contract"] = template_contract["receipt_contract"]
    if "audio_cache_contract" in template_contract:
        item["audio_cache_contract"] = template_contract["audio_cache_contract"]
    return item


def _operational_template_groups_payload(
    templates: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    total = 0
    configured = 0
    approved = 0
    blocking = 0
    pending = 0
    rejected = 0
    webviews = 0
    flow_candidates = 0
    catalog_candidates = 0
    fallback_contracts = 0
    receipt_contracts = 0
    qa_case_links = 0
    audio_cache_contracts = 0

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
            blocking += 1 if item["readiness"].get("severity") == "blocking" else 0
            pending += 1 if item["status"].get("pending") else 0
            rejected += 1 if item["status"].get("rejected") else 0
            webviews += 1 if item.get("requires_webview") else 0
            flow_candidates += 1 if item["readiness"].get("requires_whatsapp_flow_design") else 0
            catalog_candidates += 1 if item["readiness"].get("requires_catalog_sync") else 0
            fallback_contracts += 1 if item.get("fallback") else 0
            receipt_contracts += 1 if item.get("receipt_contract") else 0
            qa_case_links += len(item.get("qa_cases") or [])
            audio_cache_contracts += 1 if item.get("audio_cache_contract") else 0

        groups[group_id] = {
            "label": group["label"],
            "purpose": group["purpose"],
            "items": items,
            "summary": {
                "total": len(items),
                "configured": sum(1 for item in items if item["status"].get("configured")),
                "approved": sum(1 for item in items if item["status"].get("approved")),
                "blocking": sum(1 for item in items if item["readiness"].get("severity") == "blocking"),
                "pending": sum(1 for item in items if item["status"].get("pending")),
                "rejected": sum(1 for item in items if item["status"].get("rejected")),
                "webviews": sum(1 for item in items if item.get("requires_webview")),
                "whatsapp_flow_candidates": sum(1 for item in items if item["readiness"].get("requires_whatsapp_flow_design")),
                "catalog_candidates": sum(1 for item in items if item["readiness"].get("requires_catalog_sync")),
                "fallback_contracts": sum(1 for item in items if item.get("fallback")),
                "receipt_contracts": sum(1 for item in items if item.get("receipt_contract")),
                "qa_case_links": sum(len(item.get("qa_cases") or []) for item in items),
                "audio_cache_contracts": sum(1 for item in items if item.get("audio_cache_contract")),
            },
        }

    return {
        "groups": groups,
        "summary": {
            "total_catalog_items": total,
            "configured": configured,
            "approved": approved,
            "missing": total - configured,
            "blocking": blocking,
            "pending": pending,
            "rejected": rejected,
            "webviews": webviews,
            "whatsapp_flow_candidates": flow_candidates,
            "catalog_candidates": catalog_candidates,
            "fallback_contracts": fallback_contracts,
            "receipt_contracts": receipt_contracts,
            "qa_case_links": qa_case_links,
            "audio_cache_contracts": audio_cache_contracts,
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
            "suggested_actions": ["open_case", "commerce", "human_handoff"],
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
        _enrich_blueprint_item(item)
        _attach_template_status_and_readiness(item, templates)
        status_payload = item["status"]
        configured += 1 if status_payload.get("configured") else 0
        approved += 1 if status_payload.get("approved") else 0

    vertical_configured = 0
    vertical_approved = 0
    for items in vertical_templates.values():
        for item in items:
            item["friendly_name"] = CHATBOC_TEMPLATE_FRIENDLY_NAMES.get(_lower(item["id"]))
            _enrich_blueprint_item(item)
            _attach_template_status_and_readiness(item, templates)
            status_payload = item["status"]
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
    operational_manifest_items: list[Mapping[str, Any]] = []
    for group in operational_catalog["groups"].values():
        if isinstance(group, Mapping) and isinstance(group.get("items"), list):
            operational_manifest_items.extend(
                item for item in group["items"] if isinstance(item, Mapping)
            )
    readiness_action_items: list[dict[str, Any]] = []
    readiness_action_items.extend(
        _readiness_action_item(item, group="required")
        for item in required_templates
    )
    for vertical, items in vertical_templates.items():
        readiness_action_items.extend(
            _readiness_action_item(item, group=f"vertical:{vertical}")
            for item in items
        )
    for group_id, group in operational_catalog["groups"].items():
        readiness_action_items.extend(
            _readiness_action_item(item, group=f"operational:{group_id}")
            for item in group["items"]
        )
    creation_manifest = _template_creation_manifest_payload(
        tenant=tenant,
        required_templates=required_templates,
        vertical_templates=vertical_templates,
        operational_templates=operational_manifest_items,
    )

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
            "operational_blocking": operational_catalog["summary"]["blocking"],
            "operational_pending": operational_catalog["summary"]["pending"],
            "operational_rejected": operational_catalog["summary"]["rejected"],
            "operational_webviews": operational_catalog["summary"]["webviews"],
            "operational_whatsapp_flow_candidates": operational_catalog["summary"]["whatsapp_flow_candidates"],
            "operational_catalog_candidates": operational_catalog["summary"]["catalog_candidates"],
            "operational_fallback_contracts": operational_catalog["summary"]["fallback_contracts"],
            "operational_receipt_contracts": operational_catalog["summary"]["receipt_contracts"],
            "operational_qa_case_links": operational_catalog["summary"]["qa_case_links"],
            "operational_audio_cache_contracts": operational_catalog["summary"]["audio_cache_contracts"],
        },
        "next_actions": _sorted_readiness_actions(readiness_action_items),
        "creation_manifest": creation_manifest,
        "twilio_content_types": {
            "transactional": ["twilio/text", "twilio/call-to-action", "twilio/quick-reply"],
            "menus": ["twilio/list-picker", "twilio/quick-reply"],
            "checkout": ["twilio/call-to-action"],
            "catalog": ["twilio/call-to-action", "twilio/list-picker"],
            "fallback": ["twilio/text"],
        },
        "meta_business_strategy": {
            "whatsapp_flows": {
                "recommended_for": ["reclamos_guiados", "tramites", "admisiones", "soporte", "encuestas"],
                "use_when": "el usuario debe completar datos estructurados sin salir de WhatsApp",
                "backend_contract": "persistir borrador, validar campos y confirmar por webhook/API antes de crear expediente final",
            },
            "commerce_catalog": {
                "recommended_for": ["catalogos_pyme", "pedidos_recurrentes", "combos", "promociones"],
                "use_when": "hay catalogo con stock, imagenes y precios suficientes",
                "backend_contract": "sincronizar CatalogoItem, carrito y analytics de funnel",
            },
            "signed_webviews": {
                "recommended_for": ["pagos", "checkout", "seguimiento_reclamo", "documentos", "formularios_largos"],
                "use_when": "se requiere UX rica, pago seguro o datos sensibles",
                "backend_contract": "URL firmada con tenant, ticket/pedido, expiracion y confirmacion server-to-server",
            },
            "argentina_payments_note": "Mantener checkout/webview propio hasta confirmar disponibilidad de pagos nativos de WhatsApp para el pais y cuenta.",
        },
        "template_creation_payload_hint": {
            "language": "es",
            "approval_categories": ["UTILITY", "MARKETING", "AUTHENTICATION"],
            "variables_format": "{{1}}, {{2}}, {{3}}",
            "sample_values_required": True,
            "submit_to_meta_after_create": True,
            "prefer": ["twilio/call-to-action para webviews", "twilio/quick-reply para decisiones cortas", "twilio/list-picker para menus largos"],
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


def _webview_executable_contract(flow: Mapping[str, Any]) -> dict[str, Any]:
    flow_id = str(flow.get("id") or "")
    required_fields = [str(item) for item in flow.get("requires", []) if item]
    signed_params = [str(item) for item in flow.get("signed_params", []) if item]
    confirmations = [str(item) for item in flow.get("server_confirmation", []) if item]
    template_ids = [str(item) for item in flow.get("template_ids", []) if item]
    surface = str(flow.get("surface") or "whatsapp_cta_webview")

    return {
        "contract_version": "whatsapp.webview.executable_contract.v1",
        "trigger": {
            "surface": surface,
            "template_ids": template_ids,
            "entrypoints": ["whatsapp_template_cta", "widget_action", "admin_crm_reply"],
        },
        "preconditions": {
            "required_fields": required_fields,
            "signed_params": signed_params,
            "tenant_access_enabled": True,
            "requires_signed_session": bool(signed_params),
            "requires_idempotency_key": True,
            "template_or_24h_policy": "approved_template_outside_24h_freeform_inside_window",
        },
        "backend_actions": [
            "resolve_tenant_and_contact",
            "create_signed_webview_session",
            "persist_interaction_event",
            "open_or_update_crm_record",
            "await_server_to_server_confirmation",
        ],
        "crm_writebacks": confirmations,
        "fallback": {
            "mode": str(flow.get("fallback") or "plain_text_inside_24h"),
            "show_operator_prompt": True,
            "preserve_ticket_or_order_context": True,
        },
        "qa_assertions": [
            f"{flow_id}:signed_session_created",
            f"{flow_id}:no_sensitive_data_in_chat",
            f"{flow_id}:crm_timeline_updated",
            f"{flow_id}:fallback_available",
        ],
        "frontend_contract": {
            "render_as": "whatsapp_flow_execution_card",
            "show_preconditions": True,
            "show_crm_writebacks": True,
            "show_qa_assertions": True,
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
    claim_tracking_url = "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}"
    order_tracking_url = "/api/public/tracking/experience?kind=order&code={code}"
    checkout_session_url = endpoints.get("public_checkout_session") or "/api/checkout/crear-preferencia"
    survey_public_url = "/e/{survey_slug}"
    flows = [
        {
            "id": "claim_tracking_helpdesk",
            "label": "Seguimiento de reclamo con mesa de ayuda",
            "verticals": ["gobierno", "consorcio"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["gov_claim_created", "gov_claim_status_update", "case_created"],
            "url_template": claim_tracking_url,
            "requires": ["code", "pin"],
            "signed_params": ["tenant_slug", "ticket_id", "pin", "expires_at"],
            "server_confirmation": ["public_comment_created", "ticket_timeline_refreshed"],
            "fallback": "plain_tracking_url_with_pin",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "order_checkout",
            "label": "Checkout seguro de pedido",
            "verticals": ["pyme", "colegio"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["order_checkout", "pyme_order_ready", "pyme_payment_link", "school_payment_due"],
            "url_template": checkout_session_url,
            "requires": ["order_code", "session_token"],
            "signed_params": ["tenant_slug", "order_id", "amount", "expires_at"],
            "server_confirmation": ["payment_webhook", "order_status_updated"],
            "fallback": "payment_link_text_inside_24h",
            "status": "ready" if checkout_experience.get("ready") and integration_access.get("enabled") else "needs_checkout_setup",
        },
        {
            "id": "finance_onboarding_kyc",
            "label": "Alta digital con KYC y biometria",
            "verticals": ["finanzas", "cooperativa", "mutual", "gobierno", "pyme"],
            "surface": "whatsapp_flow_or_signed_webview",
            "template_ids": ["finance_account_onboarding", "finance_kyc_review"],
            "url_template": "/finanzas/{tenant_slug}/alta/{operation_code}?session={session_token}",
            "requires": ["tenant_slug", "operation_code", "session_token"],
            "signed_params": ["tenant_slug", "operation_code", "contact_key", "expires_at"],
            "server_confirmation": ["identity_verified", "onboarding_submitted", "crm_lead_updated"],
            "fallback": "human_identity_review_inside_crm",
            "security": {
                "card_data_in_chat": False,
                "identity_data_in_chat": False,
                "requires_consent": True,
                "audit_trail": True,
            },
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "finance_credit_collection_signature",
            "label": "Credito, cobranza, pago y firma",
            "verticals": ["finanzas", "cooperativa", "mutual", "gobierno", "pyme", "colegio"],
            "surface": "whatsapp_cta_webview",
            "template_ids": [
                "finance_credit_offer",
                "finance_collection_due",
                "finance_secure_payment",
                "finance_document_signature",
            ],
            "url_template": "/finanzas/{tenant_slug}/operacion/{operation_code}?session={session_token}",
            "requires": ["tenant_slug", "operation_code", "session_token"],
            "signed_params": ["tenant_slug", "operation_code", "amount", "contact_key", "expires_at"],
            "server_confirmation": ["payment_webhook", "signature_completed", "crm_operation_updated"],
            "fallback": "secure_payment_or_signature_link_inside_24h",
            "security": {
                "card_data_in_chat": False,
                "confirmation_source": "server_to_server_webhook",
                "requires_idempotency_key": True,
                "audit_trail": True,
            },
            "status": "ready" if checkout_experience.get("ready") and integration_access.get("enabled") else "needs_checkout_setup",
        },
        {
            "id": "finance_account_servicing",
            "label": "Estado de cuenta, resumen y soporte transaccional",
            "verticals": ["finanzas", "cooperativa", "mutual", "gobierno", "pyme", "colegio"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["finance_account_status", "finance_support_case"],
            "url_template": "/finanzas/{tenant_slug}/cuentas/{account_code}?session={session_token}",
            "requires": ["tenant_slug", "account_code", "session_token"],
            "signed_params": ["tenant_slug", "account_code", "contact_key", "expires_at"],
            "server_confirmation": ["statement_opened", "support_case_linked", "crm_contact_updated"],
            "fallback": "secure_statement_link_inside_24h",
            "security": {
                "balance_data_in_chat": False,
                "account_data_in_chat": False,
                "requires_signed_session": True,
                "audit_trail": True,
            },
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "finance_remittance_transfer",
            "label": "Remesas y transferencias con tracking",
            "verticals": ["finanzas", "cooperativa", "mutual", "pyme"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["finance_remittance_transfer", "finance_secure_payment"],
            "url_template": "/finanzas/{tenant_slug}/transferencias/{operation_code}?session={session_token}",
            "requires": ["tenant_slug", "operation_code", "session_token"],
            "signed_params": ["tenant_slug", "operation_code", "contact_key", "amount", "expires_at"],
            "server_confirmation": ["transfer_validated", "transfer_receipt_ready", "crm_operation_updated"],
            "fallback": "transfer_tracking_link_inside_24h",
            "security": {
                "beneficiary_data_in_chat": False,
                "requires_idempotency_key": True,
                "audit_trail": True,
            },
            "status": "ready" if checkout_experience.get("ready") and integration_access.get("enabled") else "needs_checkout_setup",
        },
        {
            "id": "finance_insurance_claim",
            "label": "Seguro, siniestro y documentacion",
            "verticals": ["finanzas", "seguros", "mutual", "cooperativa", "gobierno"],
            "surface": "whatsapp_flow_or_signed_webview",
            "template_ids": ["finance_insurance_claim", "finance_document_signature", "finance_support_case"],
            "url_template": "/finanzas/{tenant_slug}/seguros/{claim_code}?session={session_token}",
            "requires": ["tenant_slug", "claim_code", "session_token"],
            "signed_params": ["tenant_slug", "claim_code", "contact_key", "expires_at"],
            "server_confirmation": ["insurance_claim_created", "attachments_uploaded", "crm_case_updated"],
            "fallback": "manual_insurance_review_inside_crm",
            "security": {
                "policy_data_in_chat": False,
                "attachment_review_required": True,
                "audit_trail": True,
            },
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "finance_fee_financing_tax",
            "label": "Financiacion de cuotas, tasas e impuestos",
            "verticals": ["gobierno", "colegio", "club", "consorcio", "finanzas"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["finance_fee_financing", "finance_tax_payment", "finance_document_signature"],
            "url_template": "/finanzas/{tenant_slug}/financiacion/{operation_code}?session={session_token}",
            "requires": ["tenant_slug", "operation_code", "session_token"],
            "signed_params": ["tenant_slug", "operation_code", "amount", "contact_key", "expires_at"],
            "server_confirmation": ["financing_plan_requested", "payment_webhook", "signature_completed"],
            "fallback": "manual_payment_plan_review_inside_crm",
            "security": {
                "card_data_in_chat": False,
                "requires_consent": True,
                "audit_trail": True,
            },
            "status": "ready" if checkout_experience.get("ready") and integration_access.get("enabled") else "needs_checkout_setup",
        },
        {
            "id": "survey_vote",
            "label": "Encuesta o votacion publica",
            "verticals": ["gobierno", "pyme", "colegio"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["survey_invite", "gov_survey_invite"],
            "url_template": survey_public_url,
            "requires": ["survey_slug"],
            "signed_params": ["tenant_slug", "survey_slug", "contact_key", "expires_at"],
            "server_confirmation": ["survey_response_saved", "analytics_updated"],
            "fallback": "survey_url_text",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "catalog_order_builder",
            "label": "Catalogo y armado de pedido",
            "verticals": ["pyme"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["pyme_catalog_invite", "order_checkout"],
            "url_template": f"/catalogo/{slug}",
            "requires": ["tenant_slug"],
            "signed_params": ["tenant_slug", "contact_key", "cart_id", "expires_at"],
            "server_confirmation": ["cart_updated", "order_created"],
            "fallback": "catalog_url_text",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "claim_live_or_offline_helpdesk",
            "label": "Chat vivo u offline asociado al reclamo",
            "verticals": ["gobierno", "consorcio", "soporte"],
            "surface": "ticket_bound_websocket_or_offline_thread",
            "template_ids": ["gov_claim_created", "gov_claim_status_update", "human_handoff", "standard_handoff"],
            "url_template": "/tracking/claim/{nro_ticket}?pin={pin}&mode=claim_helpdesk",
            "requires": ["ticket_id", "nro_ticket", "pin", "tenant_slug"],
            "signed_params": ["tenant_slug", "ticket_id", "nro_ticket", "pin", "contact_key", "expires_at"],
            "server_confirmation": ["message_saved_to_ticket_timeline", "admin_inbox_unread_incremented"],
            "availability": {
                "default_business_hours": "09:00-13:00",
                "outside_hours_mode": "offline_message",
                "tenant_admin_configurable": True,
            },
            "fallback": "offline_public_comment_on_ticket",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "government_procedure_intake",
            "label": "Tramite municipal guiado",
            "verticals": ["gobierno"],
            "surface": "whatsapp_flow_or_signed_webview",
            "template_ids": ["gov_procedure_status", "turnero_government_procedure_webview"],
            "url_template": "/tramites/{tenant_slug}/{procedure_slug}?session={session_token}",
            "requires": ["tenant_slug", "procedure_slug", "session_token"],
            "signed_params": ["tenant_slug", "procedure_slug", "contact_key", "expires_at"],
            "server_confirmation": ["procedure_draft_saved", "case_created_after_validation"],
            "fallback": "guided_chat_form_inside_24h",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "school_payment_receipt",
            "label": "Cuota escolar, comprobante y recibo",
            "verticals": ["colegio"],
            "surface": "whatsapp_cta_webview",
            "template_ids": ["school_payment_due", "school_tuition_due", "school_receipt_ready"],
            "url_template": "/colegio/{tenant_slug}/pagos/{student_id}?session={session_token}",
            "requires": ["tenant_slug", "student_id", "session_token"],
            "signed_params": ["tenant_slug", "family_id", "student_id", "amount", "expires_at"],
            "server_confirmation": ["payment_webhook", "receipt_uploaded_or_generated"],
            "fallback": "admin_offline_payment_review",
            "status": "ready" if checkout_experience.get("ready") and integration_access.get("enabled") else "needs_checkout_setup",
        },
        {
            "id": "appointment_reschedule",
            "label": "Turno, confirmacion y reprogramacion",
            "verticals": ["gobierno", "colegio", "clinica"],
            "surface": "quick_reply_then_signed_webview",
            "template_ids": ["gov_turn_confirmation", "gov_turn_reminder", "school_admin_turn", "clinic_turn_reminder"],
            "url_template": "/turnos/{tenant_slug}/{turn_code}?session={session_token}",
            "requires": ["tenant_slug", "turn_code", "session_token"],
            "signed_params": ["tenant_slug", "turn_code", "contact_key", "expires_at"],
            "server_confirmation": ["turn_confirmed", "turn_rescheduled", "admin_calendar_updated"],
            "fallback": "quick_reply_confirm_or_cancel",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
        {
            "id": "document_delivery",
            "label": "Documento, certificado o constancia disponible",
            "verticals": ["gobierno", "colegio"],
            "surface": "signed_download_webview",
            "template_ids": ["gov_document_ready", "school_certificate_ready", "school_receipt_ready"],
            "url_template": "/documentos/{tenant_slug}/{document_code}?session={session_token}",
            "requires": ["tenant_slug", "document_code", "session_token"],
            "signed_params": ["tenant_slug", "document_code", "contact_key", "expires_at"],
            "server_confirmation": ["document_opened", "download_audited"],
            "fallback": "admin_send_document_from_inbox",
            "status": "ready" if integration_access.get("enabled") else "blocked_by_access",
        },
    ]
    meta_flow_designs = {
        "claim_tracking_helpdesk": {
            "flow_name": "chatboc_claim_tracking_helpdesk",
            "category": "CUSTOMER_SUPPORT",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "ticket_summary", "title": "Resumen del reclamo", "components": ["status_badge", "timeline", "copy_ticket"]},
                {"id": "support_options", "title": "Mesa de ayuda", "components": ["live_or_offline_state", "comment_box", "map_link"]},
                {"id": "confirmation", "title": "Comentario enviado", "components": ["receipt", "next_update_hint"]},
            ],
            "completion_event": "public_comment_created",
            "data_contract": ["ticket_id", "pin", "tenant_slug", "service_window"],
        },
        "order_checkout": {
            "flow_name": "chatboc_order_checkout",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "cart_review", "title": "Revisar pedido", "components": ["items", "stock", "subtotal"]},
                {"id": "customer_data", "title": "Datos de entrega", "components": ["name", "phone", "address", "notes"]},
                {"id": "payment_or_confirm", "title": "Confirmar", "components": ["payment_status", "submit_order"]},
            ],
            "completion_event": "order_created",
            "data_contract": ["order_id", "cart_id", "customer_profile", "payment_state"],
        },
        "finance_onboarding_kyc": {
            "flow_name": "chatboc_finance_onboarding_kyc",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "consent", "title": "Consentimiento", "components": ["privacy_notice", "accept_terms", "start"]},
                {"id": "identity", "title": "Identidad", "components": ["document_upload", "selfie_or_biometric_vendor", "contact_data"]},
                {"id": "review", "title": "Revision", "components": ["risk_status", "manual_review_state", "crm_lead_link"]},
            ],
            "completion_event": "onboarding_submitted",
            "data_contract": ["operation_code", "contact_key", "consent_version", "identity_status"],
        },
        "finance_credit_collection_signature": {
            "flow_name": "chatboc_finance_credit_collection_signature",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "operation_summary", "title": "Operacion", "components": ["amount", "concept", "installments", "terms"]},
                {"id": "payment_or_plan", "title": "Pago", "components": ["secure_checkout", "payment_plan_request", "receipt_upload"]},
                {"id": "signature", "title": "Firma", "components": ["document_preview", "signature_provider", "completion_receipt"]},
            ],
            "completion_event": "crm_operation_updated",
            "data_contract": ["operation_code", "amount", "payment_state", "signature_state", "idempotency_key"],
        },
        "finance_account_servicing": {
            "flow_name": "chatboc_finance_account_servicing",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "account_gate", "title": "Acceso seguro", "components": ["otp_or_magic_link", "consent", "contact_check"]},
                {"id": "statement", "title": "Resumen", "components": ["status", "movements_summary", "download_statement"]},
                {"id": "support", "title": "Soporte", "components": ["case_picker", "comment_box", "human_handoff"]},
            ],
            "completion_event": "statement_opened",
            "data_contract": ["account_code", "contact_key", "statement_period", "support_case_id"],
        },
        "finance_remittance_transfer": {
            "flow_name": "chatboc_finance_remittance_transfer",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "transfer_summary", "title": "Transferencia", "components": ["amount", "beneficiary_alias", "fees"]},
                {"id": "validation", "title": "Validacion", "components": ["risk_status", "confirm_identity", "terms"]},
                {"id": "receipt", "title": "Comprobante", "components": ["status", "download_receipt", "support_case"]},
            ],
            "completion_event": "transfer_receipt_ready",
            "data_contract": ["operation_code", "contact_key", "amount", "beneficiary_ref", "transfer_state"],
        },
        "finance_insurance_claim": {
            "flow_name": "chatboc_finance_insurance_claim",
            "category": "CUSTOMER_SUPPORT",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "claim_type", "title": "Siniestro", "components": ["claim_picker", "policy_hint", "start"]},
                {"id": "documents", "title": "Documentacion", "components": ["photo_upload", "pdf_upload", "voice_note"]},
                {"id": "tracking", "title": "Seguimiento", "components": ["claim_code", "timeline", "next_step"]},
            ],
            "completion_event": "insurance_claim_created",
            "data_contract": ["claim_code", "contact_key", "claim_type", "attachments", "manual_review_state"],
        },
        "finance_fee_financing_tax": {
            "flow_name": "chatboc_finance_fee_financing_tax",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "debt_summary", "title": "Concepto", "components": ["concept", "due_date", "amount"]},
                {"id": "plan_options", "title": "Opciones", "components": ["installments", "discounts", "payment_methods"]},
                {"id": "confirm", "title": "Confirmar", "components": ["secure_checkout", "signature_provider", "receipt"]},
            ],
            "completion_event": "financing_plan_requested",
            "data_contract": ["operation_code", "contact_key", "amount", "installments", "payment_state"],
        },
        "survey_vote": {
            "flow_name": "chatboc_survey_vote_live",
            "category": "SURVEY",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "survey_intro", "title": "Participar", "components": ["title", "privacy_note", "start"]},
                {"id": "questions", "title": "Responder", "components": ["single_choice", "multiple_choice", "free_text", "geo_optional"]},
                {"id": "live_results", "title": "Resultados", "components": ["bars", "total_votes", "heatmap_link"]},
            ],
            "completion_event": "survey_response_saved",
            "data_contract": ["survey_slug", "contact_key", "response_payload", "geo_permission"],
        },
        "catalog_order_builder": {
            "flow_name": "chatboc_catalog_order_builder",
            "category": "COMMERCE",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "catalog", "title": "Catalogo", "components": ["search", "categories", "product_cards"]},
                {"id": "cart", "title": "Carrito", "components": ["quantity_stepper", "promo", "remove_item"]},
                {"id": "checkout_handoff", "title": "Finalizar", "components": ["customer_data", "confirm_order"]},
            ],
            "completion_event": "cart_updated",
            "data_contract": ["tenant_slug", "catalog_items", "cart_id", "contact_key"],
        },
        "government_procedure_intake": {
            "flow_name": "chatboc_government_procedure_intake",
            "category": "UTILITY",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "procedure_select", "title": "Tramite", "components": ["procedure_picker", "requirements"]},
                {"id": "citizen_data", "title": "Datos", "components": ["identity_fields", "attachments", "address"]},
                {"id": "case_created", "title": "Gestion creada", "components": ["case_number", "office_hours", "tracking_link"]},
            ],
            "completion_event": "case_created_after_validation",
            "data_contract": ["procedure_slug", "citizen_profile", "attachments", "tenant_slug"],
        },
        "school_payment_receipt": {
            "flow_name": "chatboc_school_payment_receipt",
            "category": "TRANSACTIONAL",
            "endpoint_mode": "data_exchange",
            "screens": [
                {"id": "family_debt", "title": "Cuotas", "components": ["student_selector", "due_items", "amount"]},
                {"id": "receipt_upload", "title": "Comprobante", "components": ["payment_method", "upload", "notes"]},
                {"id": "receipt_status", "title": "Estado", "components": ["review_status", "receipt_download"]},
            ],
            "completion_event": "receipt_uploaded_or_generated",
            "data_contract": ["family_id", "student_id", "amount", "receipt_file"],
        },
    }
    for flow in flows:
        design = meta_flow_designs.get(str(flow.get("id") or ""))
        flow["executable_contract"] = _webview_executable_contract(flow)
        if design:
            flow["meta_flow_blueprint"] = {
                **design,
                "safe_for_whatsapp_flow": True,
                "fallback_surface": flow.get("surface"),
                "server_confirmation": flow.get("server_confirmation", []),
            }

    return {
        "enabled": bool(integration_access.get("enabled")),
        "respect_access_lock": True,
        "entrypoints": ["whatsapp", "widget", "web"],
        "checkout": {
            "mode": "conversation_guided_secure_webview",
            "active_entrypoint": checkout_experience.get("active_entrypoint"),
            "ready": bool(checkout_experience.get("ready")),
            "public_checkout_session": checkout_session_url,
            "public_widget_session": endpoints.get("public_widget_session") or "/api/public/widget-commerce-session",
            "payment_status": endpoints.get("admin_payment_status") or f"/api/v2/tenants/{slug}/payments/status",
            "confirmation_source": "server_to_server_webhook",
            "card_data_in_chat": False,
            "client_return_trusted": False,
        },
        "tracking": {
            "claim": claim_tracking_url,
            "order": order_tracking_url,
            "timeline_fallback": "timeline_only",
            "claim_support_component": "ticket_bound_helpdesk",
        },
        "catalog": {
            "public_catalog": f"/api/public/tenants/{slug}/catalog",
            "admin_catalog": f"/api/admin/tenants/{slug}/catalog/items",
        },
        "surveys": {
            "admin": "/api/v2/surveys",
            "public_template": survey_public_url,
        },
        "flows": flows,
        "summary": {
            "flows_total": len(flows),
            "ready_flows": len([item for item in flows if str(item.get("status")) == "ready"]),
            "transactional_flows": [
                "claim_tracking_helpdesk",
                "claim_live_or_offline_helpdesk",
                "order_checkout",
                "finance_onboarding_kyc",
                "finance_credit_collection_signature",
                "finance_account_servicing",
                "finance_remittance_transfer",
                "finance_insurance_claim",
                "finance_fee_financing_tax",
                "survey_vote",
                "catalog_order_builder",
                "government_procedure_intake",
                "school_payment_receipt",
                "appointment_reschedule",
                "document_delivery",
            ],
            "requires_signed_session": True,
            "requires_server_confirmation": True,
            "meta_flow_blueprints": len(meta_flow_designs),
            "executable_contracts": len(flows),
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
    audio_cache: Mapping[str, Any],
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
            "fixed_menu_audio_cache": {
                "enabled": True,
                "scope": ["main_menu", "claim_categories", "survey_menu", "catalog_menu", "status_menu"],
                "purpose": "visual_and_motor_accessibility",
                "cache_key": "tenant_slug:channel:menu_id:language:voice_profile",
                "invalidate_on": ["menu_version_change", "tenant_voice_profile_change", "language_change"],
                "observability": dict(audio_cache),
            },
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


def _template_state_for_ids(
    template_blueprint: Mapping[str, Any],
    template_ids: list[str],
) -> dict[str, Any]:
    groups = template_blueprint.get("operational_template_groups")
    group_items: list[Mapping[str, Any]] = []
    if isinstance(groups, Mapping):
        for group in groups.values():
            if isinstance(group, Mapping) and isinstance(group.get("items"), list):
                group_items.extend(item for item in group["items"] if isinstance(item, Mapping))

    required = template_blueprint.get("required_templates")
    if isinstance(required, list):
        group_items.extend(item for item in required if isinstance(item, Mapping))

    vertical_templates = template_blueprint.get("vertical_templates")
    if isinstance(vertical_templates, Mapping):
        for items in vertical_templates.values():
            if isinstance(items, list):
                group_items.extend(item for item in items if isinstance(item, Mapping))

    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for template_id in template_ids:
        match = next((item for item in group_items if _lower(item.get("id")) == _lower(template_id)), None)
        if not match:
            missing.append(template_id)
            continue
        readiness = match.get("readiness") if isinstance(match.get("readiness"), Mapping) else {}
        status = match.get("status") if isinstance(match.get("status"), Mapping) else {}
        found.append(
            {
                "id": template_id,
                "state": readiness.get("state"),
                "severity": readiness.get("severity"),
                "approved": bool(status.get("approved")),
                "content_sid": status.get("content_sid"),
            }
        )

    blocking = [item for item in found if item.get("severity") == "blocking"]
    pending = [item for item in found if item.get("severity") in {"warning", "ready_with_dependency"}]
    return {
        "templates": found,
        "missing": missing,
        "approved": len([item for item in found if item.get("approved")]),
        "blocking": len(blocking) + len(missing),
        "pending": len(pending),
        "ready": bool(found) and not blocking and not missing,
    }


def _flow_state_for_id(webview_blueprint: Mapping[str, Any], flow_id: str) -> dict[str, Any]:
    flows = webview_blueprint.get("flows") if isinstance(webview_blueprint.get("flows"), list) else []
    flow = next((item for item in flows if isinstance(item, Mapping) and _lower(item.get("id")) == _lower(flow_id)), None)
    if not flow:
        return {
            "id": flow_id,
            "status": "missing",
            "ready": False,
            "meta_flow_blueprint_ready": False,
            "screens_count": 0,
            "data_contract_count": 0,
        }
    status = str(flow.get("status") or "review")
    meta_flow = flow.get("meta_flow_blueprint") if isinstance(flow.get("meta_flow_blueprint"), Mapping) else {}
    screens = meta_flow.get("screens") if isinstance(meta_flow.get("screens"), list) else []
    data_contract = meta_flow.get("data_contract") if isinstance(meta_flow.get("data_contract"), list) else []
    return {
        "id": flow_id,
        "status": status,
        "ready": status == "ready",
        "url_template": flow.get("url_template"),
        "surface": flow.get("surface"),
        "meta_flow_blueprint_ready": bool(meta_flow and screens and data_contract),
        "meta_flow_name": meta_flow.get("flow_name"),
        "meta_flow_category": meta_flow.get("category"),
        "endpoint_mode": meta_flow.get("endpoint_mode"),
        "screens_count": len(screens),
        "screen_ids": [str(screen.get("id") or "") for screen in screens if isinstance(screen, Mapping) and screen.get("id")],
        "data_contract_count": len(data_contract),
        "data_contract": [str(item) for item in data_contract],
        "completion_event": meta_flow.get("completion_event"),
    }


def _qa_playbook_payload(
    tenant: TenantProfile,
    *,
    channel_ready: bool,
    template_blueprint: Mapping[str, Any],
    webview_blueprint: Mapping[str, Any],
    integration_access: Mapping[str, Any],
) -> dict[str, Any]:
    scenarios = [
        {
            "id": "gov_claim_text_to_tracking",
            "label": "Reclamo municipal completo",
            "verticals": ["gobierno", "municipio"],
            "persona": "vecino",
            "entrypoint": "whatsapp",
            "templates": ["welcome_menu", "gov_claim_sla", "gov_claim_created", "gov_claim_status_update"],
            "webview_flow": "claim_tracking_helpdesk",
            "covers": ["texto", "ubicacion", "foto", "dni", "pin", "estado_reclamo", "mesa_ayuda"],
            "script_cases": [
                "junin_texto_reclamo",
                "junin_imagen",
                "junin_dni_reclamo",
                "junin_confirmacion_reclamo",
            ],
        },
        {
            "id": "gov_claim_location_to_tracking",
            "label": "Reclamo municipal con ubicacion",
            "verticals": ["gobierno", "municipio"],
            "persona": "vecino",
            "entrypoint": "whatsapp_location",
            "templates": ["welcome_menu", "gov_claim_sla", "gov_claim_created", "gov_claim_status_update"],
            "webview_flow": "claim_tracking_helpdesk",
            "covers": ["ubicacion", "geopunto", "sin_foto", "datos_contacto", "pin", "estado_reclamo"],
            "script_cases": [
                "junin_ubicacion_inicio",
                "junin_ubicacion_compartida",
                "junin_ubicacion_sin_foto",
                "junin_ubicacion_datos",
                "junin_ubicacion_confirmar",
            ],
        },
        {
            "id": "gov_claim_audio_accessible",
            "label": "Reclamo accesible por audio",
            "verticals": ["gobierno", "municipio"],
            "persona": "vecino_accesibilidad",
            "entrypoint": "whatsapp_audio",
            "templates": ["welcome_menu", "gov_claim_sla", "gov_claim_created"],
            "webview_flow": "claim_tracking_helpdesk",
            "covers": ["audio_cache", "transcripcion", "sin_foto", "confirmacion", "seguimiento"],
            "script_cases": ["junin_audio", "junin_audio_sin_foto", "junin_audio_datos", "junin_audio_confirmar"],
        },
        {
            "id": "pyme_catalog_order_checkout",
            "label": "Catalogo, pedido y checkout pyme",
            "verticals": ["pyme", "empresa"],
            "persona": "cliente",
            "entrypoint": "whatsapp",
            "templates": ["pyme_catalog_invite", "order_checkout", "pyme_order_ready", "pyme_payment_link"],
            "webview_flow": "catalog_order_builder",
            "covers": ["catalogo", "carrito", "pedido", "checkout", "tracking_pedido"],
            "script_cases": [
                "cuatro_fincas_pedido",
                "cuatro_fincas_confirmar",
            ],
        },
        {
            "id": "chatboc_demo_hub",
            "label": "Hub comercial Chatboc",
            "verticals": ["platform", "gobierno", "colegio", "pyme"],
            "persona": "prospecto",
            "entrypoint": "whatsapp_demo",
            "templates": ["welcome_menu", "pyme_catalog_invite", "survey_invite"],
            "webview_flow": "catalog_order_builder",
            "covers": ["demo_menu", "rubro", "lead_crm", "pedido_demo", "encuestas", "ventas"],
            "script_cases": [
                "chatboc_demo_menu",
                "chatboc_demo_empresas",
                "chatboc_demo_order_start",
                "chatboc_demo_order_confirm",
            ],
        },
        {
            "id": "survey_vote_realtime",
            "label": "Encuesta o votacion en vivo",
            "verticals": ["gobierno", "colegio", "pyme"],
            "persona": "participante",
            "entrypoint": "whatsapp_cta_webview",
            "templates": ["survey_invite", "gov_survey_invite"],
            "webview_flow": "survey_vote",
            "covers": ["invitacion", "voto", "resultados_en_vivo", "analytics_heatmap"],
            "script_cases": ["chatboc_demo_surveys", "chatboc_demo_surveys_empresas", "chatboc_demo_survey_open"],
        },
        {
            "id": "school_family_case",
            "label": "Caso familiar colegio",
            "verticals": ["colegio", "educacion"],
            "persona": "familia",
            "entrypoint": "whatsapp",
            "templates": ["school_family_case_created", "school_payment_due", "school_receipt_ready"],
            "webview_flow": "order_checkout",
            "covers": ["menu_familia", "inasistencia", "certificado_audio", "cuotas", "comprobante"],
            "script_cases": ["colegio_menu_sandbox", "colegio_seleccion_inasistencia", "colegio_detalle_audio_ubicacion"],
        },
        {
            "id": "finance_onboarding_collection_signature",
            "label": "Alta financiera, cobranza y firma in-chat",
            "verticals": ["finanzas", "cooperativa", "mutual", "gobierno", "colegio", "pyme"],
            "persona": "cliente",
            "entrypoint": "whatsapp_cta_webview",
            "templates": [
                "finance_account_onboarding",
                "finance_kyc_review",
                "finance_credit_offer",
                "finance_collection_due",
                "finance_secure_payment",
                "finance_document_signature",
            ],
            "webview_flow": "finance_credit_collection_signature",
            "covers": ["consentimiento", "kyc", "oferta", "cobranza", "pago_seguro", "firma", "webhook_crm"],
            "script_cases": [
                "finance_alta_digital",
                "finance_revision_kyc",
                "finance_cobranza_pago_firma",
            ],
        },
        {
            "id": "finance_account_servicing",
            "label": "Estado de cuenta y soporte seguro",
            "verticals": ["finanzas", "cooperativa", "mutual", "gobierno", "colegio", "pyme"],
            "persona": "cliente",
            "entrypoint": "whatsapp_cta_webview",
            "templates": ["finance_account_status", "finance_support_case"],
            "webview_flow": "finance_account_servicing",
            "covers": ["estado_cuenta", "resumen", "soporte", "handoff_crm", "auditoria"],
            "script_cases": [
                "finance_account_status",
                "finance_support_handoff",
            ],
        },
        {
            "id": "finance_remittance_transfer",
            "label": "Remesa o transferencia con tracking",
            "verticals": ["finanzas", "cooperativa", "mutual", "pyme"],
            "persona": "cliente",
            "entrypoint": "whatsapp_cta_webview",
            "templates": ["finance_remittance_transfer", "finance_secure_payment"],
            "webview_flow": "finance_remittance_transfer",
            "covers": ["transferencia", "validacion", "comprobante", "tracking", "webhook_crm"],
            "script_cases": [
                "finance_remittance_transfer",
                "finance_transfer_receipt",
            ],
        },
        {
            "id": "finance_insurance_claim",
            "label": "Seguro o siniestro con documentacion",
            "verticals": ["finanzas", "seguros", "mutual", "cooperativa", "gobierno"],
            "persona": "asegurado",
            "entrypoint": "whatsapp_image_or_document",
            "templates": ["finance_insurance_claim", "finance_document_signature", "finance_support_case"],
            "webview_flow": "finance_insurance_claim",
            "covers": ["tipo_siniestro", "foto", "pdf", "voz", "seguimiento", "mesa_ayuda"],
            "script_cases": [
                "finance_insurance_claim",
                "finance_insurance_document",
            ],
        },
        {
            "id": "finance_fee_financing_tax",
            "label": "Financiacion de cuotas, tasas e impuestos",
            "verticals": ["gobierno", "colegio", "club", "consorcio", "finanzas"],
            "persona": "pagador",
            "entrypoint": "whatsapp_cta_webview",
            "templates": ["finance_fee_financing", "finance_tax_payment", "finance_document_signature"],
            "webview_flow": "finance_fee_financing_tax",
            "covers": ["deuda", "plan_pago", "descuento", "pago_seguro", "firma", "recibo"],
            "script_cases": [
                "finance_fee_financing",
                "finance_tax_payment",
            ],
        },
    ]

    enriched: list[dict[str, Any]] = []
    for scenario in scenarios:
        template_state = _template_state_for_ids(template_blueprint, list(scenario["templates"]))
        flow_state = _flow_state_for_id(webview_blueprint, str(scenario.get("webview_flow")))
        ready = bool(channel_ready and integration_access.get("enabled") and template_state["ready"] and flow_state["ready"])
        if not channel_ready:
            status = "blocked_channel"
            next_action = "configure_whatsapp_sender_and_plan"
        elif template_state["blocking"]:
            status = "blocked_templates"
            next_action = "create_or_approve_required_templates"
        elif not flow_state["ready"]:
            status = "blocked_webview"
            next_action = "complete_signed_webview_flow"
        elif template_state["pending"]:
            status = "needs_template_review"
            next_action = "refresh_twilio_status_or_wait_for_meta_approval"
        else:
            status = "ready"
            next_action = "run_qa_scenario"
        enriched.append(
            {
                **scenario,
                "status": status,
                "ready": ready,
                "next_action": next_action,
                "template_state": template_state,
                "webview_state": flow_state,
                "meta_flow_coverage": {
                    "ready": bool(flow_state.get("meta_flow_blueprint_ready")),
                    "flow_name": flow_state.get("meta_flow_name"),
                    "category": flow_state.get("meta_flow_category"),
                    "endpoint_mode": flow_state.get("endpoint_mode"),
                    "screens": flow_state.get("screen_ids") or [],
                    "data_contract": flow_state.get("data_contract") or [],
                    "completion_event": flow_state.get("completion_event"),
                },
            }
        )

    ready_count = len([item for item in enriched if item["ready"]])
    meta_flow_ready_count = len([item for item in enriched if item["meta_flow_coverage"]["ready"]])
    return {
        "contract_version": "whatsapp.qa_playbook.v1",
        "enabled": bool(channel_ready),
        "scenario_count": len(enriched),
        "ready_count": ready_count,
        "blocked_count": len(enriched) - ready_count,
        "meta_flow_ready_count": meta_flow_ready_count,
        "local_command": "python scripts/qa_whatsapp_flows.py",
        "live_mode_env": "QA_WHATSAPP_USE_CONFIGURED_DB=1",
        "safe_default": "isolated_in_memory_db",
        "live_mode_guardrails": [
            "requiere TWILIO_AUTH_TOKEN real",
            "no envia outbound live: ejecuta inbound webhook controlado",
            "usar QA_CHATBOC_DEMO_TO y QA_JUNIN_TO para seleccionar numeros",
        ],
        "numbers": {
            "junin": "+17432643718",
            "chatboc_demos": "+18564858589",
            "twilio_sandbox": "+14155238886",
        },
        "scenarios": enriched,
        "frontend_contract": {
            "render_as": "whatsapp_qa_playbook",
            "show_ready_matrix": True,
            "show_script_cases": True,
            "show_meta_flow_coverage": True,
            "show_live_mode_warning": True,
        },
    }


def _finance_activation_plan_payload(
    tenant: TenantProfile,
    *,
    journeys: list[Mapping[str, Any]],
    finance_templates: list[Any],
    finance_flows: list[Mapping[str, Any]],
    finance_scenarios: list[Mapping[str, Any]],
    checkout_experience: Mapping[str, Any],
    integration_access: Mapping[str, Any],
) -> dict[str, Any]:
    cfg = _tenant_cfg(tenant)
    whatsapp_ready = bool(_whatsapp_number(tenant, cfg))
    access_enabled = bool(integration_access.get("enabled"))
    checkout_ready = bool(checkout_experience.get("ready"))
    flow_ids = {str(flow.get("id") or "") for flow in finance_flows if isinstance(flow, Mapping)}
    template_ids = {str(item.get("id") or "") for item in finance_templates if isinstance(item, Mapping)}
    approved_template_ids = {
        str(item.get("id") or "")
        for item in finance_templates
        if isinstance(item, Mapping)
        and isinstance(item.get("status"), Mapping)
        and bool(item["status"].get("approved"))
    }
    ready_scenario_ids = {
        str(scenario.get("id") or "")
        for scenario in finance_scenarios
        if isinstance(scenario, Mapping) and bool(scenario.get("ready"))
    }
    has_kyc_provider = bool(
        cfg.get("kyc_provider")
        or cfg.get("identity_provider")
        or cfg.get("identity_verification_provider")
    )
    has_signature_provider = bool(
        cfg.get("signature_provider")
        or cfg.get("document_signature_provider")
        or cfg.get("signature_webhook_url")
    )

    def capability(
        capability_id: str,
        label: str,
        ready: bool,
        *,
        owner: str,
        required: bool = True,
        action: str,
    ) -> dict[str, Any]:
        return {
            "id": capability_id,
            "label": label,
            "ready": bool(ready),
            "required": bool(required),
            "owner": owner,
            "action": action,
        }

    capabilities = [
        capability(
            "whatsapp_sender",
            "Remitente WhatsApp verificado",
            whatsapp_ready,
            owner="tenant_admin",
            action="Conectar numero oficial o revisar sender aprobado.",
        ),
        capability(
            "twilio_content_templates",
            "Plantillas finance aprobadas",
            len(approved_template_ids) >= min(len(template_ids), 6) and bool(template_ids),
            owner="chatboc_ops",
            action="Crear, aprobar y versionar plantillas de KYC, pago, firma y soporte.",
        ),
        capability(
            "signed_webviews",
            "Webviews firmados y Meta Flows",
            len(flow_ids) >= 6,
            owner="chatboc_engineering",
            action="Publicar webviews transaccionales con sesion firmada y data exchange.",
        ),
        capability(
            "secure_checkout_or_payment_gateway",
            "Checkout o gateway seguro",
            checkout_ready,
            owner="tenant_admin",
            action="Configurar checkout, webhook server-to-server e idempotency key.",
        ),
        capability(
            "identity_or_kyc_provider",
            "Proveedor de identidad/KYC",
            has_kyc_provider,
            owner="tenant_admin",
            action="Definir proveedor KYC o modo de revision manual auditada.",
        ),
        capability(
            "document_signature_provider",
            "Firma y documentos",
            has_signature_provider,
            owner="tenant_admin",
            action="Conectar proveedor de firma o webview de consentimiento validado.",
        ),
        capability(
            "crm_queues",
            "Colas CRM operativas",
            True,
            owner="chatboc_product",
            action="Mapear owner, SLA y handoff por cola financiera.",
        ),
        capability(
            "audit_trail",
            "Trazabilidad y auditoria",
            True,
            owner="chatboc_product",
            action="Persistir eventos de pago, KYC, firma, webview y operador.",
        ),
        capability(
            "whatsapp_qa_playbook",
            "QA reproducible WhatsApp/webview",
            len(ready_scenario_ids) >= 3,
            owner="chatboc_qa",
            action="Ejecutar playbook de finance antes de vender el tenant.",
        ),
    ]

    def launch_track(
        track_id: str,
        label: str,
        journey_ids: list[str],
        required_templates: list[str],
        webview_flow: str,
        surfaces: list[str],
        *,
        requires_checkout: bool = False,
        requires_kyc: bool = False,
        requires_signature: bool = False,
    ) -> dict[str, Any]:
        ready = (
            webview_flow in flow_ids
            and set(required_templates).issubset(template_ids)
            and (not requires_checkout or checkout_ready)
            and (not requires_kyc or has_kyc_provider)
            and (not requires_signature or has_signature_provider)
        )
        matching_journeys = [
            journey
            for journey in journeys
            if isinstance(journey, Mapping) and str(journey.get("id") or "") in journey_ids
        ]
        return {
            "id": track_id,
            "label": label,
            "journey_ids": journey_ids,
            "required_templates": required_templates,
            "webview_flow": webview_flow,
            "surfaces": surfaces,
            "ready": ready,
            "mapped_journeys": len(matching_journeys),
        }

    launch_tracks = [
        launch_track(
            "finance_core_servicing",
            "Atencion financiera y estado de cuenta",
            ["account_statement_support"],
            ["finance_account_status", "finance_support_case"],
            "finance_account_servicing",
            ["whatsapp", "secure_webview", "crm_queue", "analytics"],
        ),
        launch_track(
            "onboarding_kyc",
            "Alta digital y verificacion KYC",
            ["digital_account_opening"],
            ["finance_account_onboarding", "finance_kyc_review"],
            "finance_onboarding_kyc",
            ["whatsapp", "meta_flow", "kyc_review_queue", "audit_trail"],
            requires_kyc=True,
        ),
        launch_track(
            "collections_payments_signature",
            "Cobranzas, pagos y firma",
            ["collections_payment_plan_signature"],
            ["finance_collection_due", "finance_secure_payment", "finance_document_signature"],
            "finance_credit_collection_signature",
            ["whatsapp_template", "payment_webview", "signature_flow", "crm_queue"],
            requires_checkout=True,
            requires_signature=True,
        ),
        launch_track(
            "remittance_and_receipts",
            "Remesas, transferencias y comprobantes",
            ["remittance_transfer_tracking"],
            ["finance_remittance_transfer", "finance_secure_payment"],
            "finance_remittance_transfer",
            ["whatsapp", "receipt_webview", "operator_review", "analytics"],
            requires_checkout=True,
        ),
        launch_track(
            "insurance_claims",
            "Siniestros y documentacion",
            ["insurance_claim_documentation"],
            ["finance_insurance_claim", "finance_document_signature", "finance_support_case"],
            "finance_insurance_claim",
            ["whatsapp", "attachment_webview", "signature_flow", "support_queue"],
            requires_signature=True,
        ),
        launch_track(
            "fees_taxes_school_government",
            "Cuotas, tasas e impuestos",
            ["fees_taxes_financing"],
            ["finance_fee_financing", "finance_tax_payment", "finance_document_signature"],
            "finance_fee_financing_tax",
            ["whatsapp", "payment_plan_webview", "public_sector_crm", "analytics"],
            requires_checkout=True,
            requires_signature=True,
        ),
    ]

    next_actions: list[dict[str, Any]] = []
    for item in capabilities:
        if not item["ready"] and item["required"]:
            next_actions.append(
                {
                    "id": f"activate_{item['id']}",
                    "severity": "blocking",
                    "owner": item["owner"],
                    "label": item["label"],
                    "action": item["action"],
                }
            )

    if not next_actions:
        next_actions.append(
            {
                "id": "run_finance_pilot",
                "severity": "ready",
                "owner": "chatboc_ops",
                "label": "Pilot finance listo",
                "action": "Ejecutar pruebas end-to-end con tenant piloto y aprobar salida comercial.",
            }
        )

    ready_tracks = len([track for track in launch_tracks if track["ready"]])
    blocking_count = len([action for action in next_actions if action["severity"] == "blocking"])
    go_live_state = "ready" if blocking_count == 0 else "blocked"

    return {
        "contract_version": "finance.activation_plan.v1",
        "go_live_state": go_live_state,
        "ready_tracks": ready_tracks,
        "blocking_count": blocking_count,
        "required_capabilities": capabilities,
        "launch_tracks": launch_tracks,
        "next_actions": next_actions,
        "setup_questions": [
            {
                "id": "finance_segment",
                "label": "Rubro financiero",
                "prompt": "Banco, fintech, colegio, municipio, mutual, cooperativa, pyme o cobranzas.",
            },
            {
                "id": "provider_stack",
                "label": "Stack de proveedores",
                "prompt": "Gateway de pago, KYC, firma, ERP/core, horarios y responsable operativo.",
            },
            {
                "id": "service_catalog",
                "label": "Catalogo transaccional",
                "prompt": "Productos, cuotas, tasas, creditos, remesas, seguros, certificados o comprobantes.",
            },
            {
                "id": "human_escalation",
                "label": "Escalamiento humano",
                "prompt": "Colas, SLA, horarios, mensajes offline y reglas de derivacion por riesgo.",
            },
        ],
        "frontend_contract": {
            "render_as": "finance_activation_plan",
            "show_capabilities": True,
            "show_launch_tracks": True,
            "show_next_actions": True,
            "show_setup_questions": True,
        },
    }


def _finance_transactional_payload(
    tenant: TenantProfile,
    *,
    template_blueprint: Mapping[str, Any],
    webview_blueprint: Mapping[str, Any],
    qa_playbook: Mapping[str, Any],
    checkout_experience: Mapping[str, Any],
    integration_access: Mapping[str, Any],
) -> dict[str, Any]:
    template_groups = (
        template_blueprint.get("operational_template_groups")
        if isinstance(template_blueprint.get("operational_template_groups"), Mapping)
        else {}
    )
    finance_group = template_groups.get("financial_services") if isinstance(template_groups, Mapping) else {}
    finance_templates = finance_group.get("items") if isinstance(finance_group, Mapping) and isinstance(finance_group.get("items"), list) else []
    flows = webview_blueprint.get("flows") if isinstance(webview_blueprint.get("flows"), list) else []
    finance_flows = [
        flow
        for flow in flows
        if isinstance(flow, Mapping) and str(flow.get("id") or "").startswith("finance_")
    ]
    qa_scenarios = qa_playbook.get("scenarios") if isinstance(qa_playbook.get("scenarios"), list) else []
    finance_scenarios = [
        scenario
        for scenario in qa_scenarios
        if isinstance(scenario, Mapping) and str(scenario.get("id") or "").startswith("finance_")
    ]
    ready_flows = [flow for flow in finance_flows if str(flow.get("status") or "").lower() == "ready"]
    ready_scenarios = [scenario for scenario in finance_scenarios if bool(scenario.get("ready"))]
    template_ids = {str(item.get("id") or "") for item in finance_templates if isinstance(item, Mapping)}
    flow_ids = {str(item.get("id") or "") for item in finance_flows if isinstance(item, Mapping)}

    journeys = [
        {
            "id": "digital_account_opening",
            "label": "Alta digital de cuenta o producto",
            "segments": ["bancos", "fintech", "cooperativas", "mutuales", "gobiernos", "pymes"],
            "templates": ["finance_account_onboarding", "finance_kyc_review"],
            "webview_flow": "finance_onboarding_kyc",
            "crm_stage": "lead_verificacion_identidad",
            "success_event": "onboarding_submitted",
            "analytics_events": ["finance_onboarding_started", "kyc_submitted", "manual_review_required"],
            "ready": "finance_onboarding_kyc" in flow_ids
            and {"finance_account_onboarding", "finance_kyc_review"}.issubset(template_ids),
        },
        {
            "id": "collections_payment_plan_signature",
            "label": "Cobranza, plan de pago, checkout y firma",
            "segments": ["finanzas", "colegios", "municipios", "clubes", "consorcios"],
            "templates": [
                "finance_collection_due",
                "finance_secure_payment",
                "finance_document_signature",
            ],
            "webview_flow": "finance_credit_collection_signature",
            "crm_stage": "operacion_transaccional",
            "success_event": "crm_operation_updated",
            "analytics_events": ["collection_opened", "payment_started", "signature_completed"],
            "ready": "finance_credit_collection_signature" in flow_ids
            and {"finance_collection_due", "finance_secure_payment", "finance_document_signature"}.issubset(template_ids),
        },
        {
            "id": "account_statement_support",
            "label": "Estado de cuenta y soporte asociado",
            "segments": ["bancos", "fintech", "cooperativas", "mutuales", "colegios", "gobiernos"],
            "templates": ["finance_account_status", "finance_support_case"],
            "webview_flow": "finance_account_servicing",
            "crm_stage": "soporte_cuenta",
            "success_event": "statement_opened",
            "analytics_events": ["statement_opened", "support_case_created", "operator_handoff"],
            "ready": "finance_account_servicing" in flow_ids
            and {"finance_account_status", "finance_support_case"}.issubset(template_ids),
        },
        {
            "id": "remittance_transfer_tracking",
            "label": "Remesa o transferencia con comprobante",
            "segments": ["fintech", "cooperativas", "mutuales", "pymes"],
            "templates": ["finance_remittance_transfer", "finance_secure_payment"],
            "webview_flow": "finance_remittance_transfer",
            "crm_stage": "transferencia_en_validacion",
            "success_event": "transfer_receipt_ready",
            "analytics_events": ["transfer_started", "transfer_validated", "transfer_receipt_ready"],
            "ready": "finance_remittance_transfer" in flow_ids
            and {"finance_remittance_transfer", "finance_secure_payment"}.issubset(template_ids),
        },
        {
            "id": "insurance_claim_documentation",
            "label": "Seguro o siniestro con documentacion",
            "segments": ["aseguradoras", "mutuales", "cooperativas", "gobiernos"],
            "templates": ["finance_insurance_claim", "finance_document_signature", "finance_support_case"],
            "webview_flow": "finance_insurance_claim",
            "crm_stage": "siniestro_documentacion",
            "success_event": "insurance_claim_created",
            "analytics_events": ["insurance_claim_started", "attachment_uploaded", "manual_review_assigned"],
            "ready": "finance_insurance_claim" in flow_ids
            and {"finance_insurance_claim", "finance_document_signature", "finance_support_case"}.issubset(template_ids),
        },
        {
            "id": "fees_taxes_financing",
            "label": "Financiacion de cuotas, tasas e impuestos",
            "segments": ["municipios", "colegios", "clubes", "consorcios", "finanzas"],
            "templates": ["finance_fee_financing", "finance_tax_payment", "finance_document_signature"],
            "webview_flow": "finance_fee_financing_tax",
            "crm_stage": "plan_pago_en_revision",
            "success_event": "financing_plan_requested",
            "analytics_events": ["debt_summary_opened", "financing_plan_requested", "tax_payment_completed"],
            "ready": "finance_fee_financing_tax" in flow_ids
            and {"finance_fee_financing", "finance_tax_payment", "finance_document_signature"}.issubset(template_ids),
        },
    ]

    activation_plan = _finance_activation_plan_payload(
        tenant,
        journeys=journeys,
        finance_templates=finance_templates,
        finance_flows=finance_flows,
        finance_scenarios=finance_scenarios,
        checkout_experience=checkout_experience,
        integration_access=integration_access,
    )

    return {
        "contract_version": "finance.transactional_whatsapp.v1",
        "enabled": bool(integration_access.get("enabled")),
        "tenant": _tenant_ref(tenant),
        "summary": {
            "templates": len(finance_templates),
            "webview_flows": len(finance_flows),
            "ready_flows": len(ready_flows),
            "qa_scenarios": len(finance_scenarios),
            "ready_qa_scenarios": len(ready_scenarios),
            "checkout_ready": bool(checkout_experience.get("ready")),
            "journeys": len(journeys),
            "ready_journeys": len([journey for journey in journeys if journey["ready"]]),
            "activation_blockers": activation_plan["blocking_count"],
            "activation_ready_tracks": activation_plan["ready_tracks"],
        },
        "journeys": journeys,
        "activation_plan": activation_plan,
        "crm_operating_model": {
            "contract_version": "finance.crm_operating_model.v1",
            "queues": [
                {"id": "identity_review", "label": "KYC y validacion manual", "sla_minutes": 240},
                {"id": "collections", "label": "Cobranzas y planes de pago", "sla_minutes": 120},
                {"id": "payments", "label": "Pagos y comprobantes", "sla_minutes": 60},
                {"id": "signature", "label": "Firma y documentacion", "sla_minutes": 180},
                {"id": "support", "label": "Soporte financiero", "sla_minutes": 240},
            ],
            "required_events": [
                "operation_created",
                "identity_verified",
                "payment_webhook",
                "signature_completed",
                "crm_operation_updated",
            ],
            "admin_actions": [
                "assign_owner",
                "request_missing_document",
                "approve_manual_review",
                "send_secure_webview",
                "close_operation",
            ],
        },
        "analytics_model": {
            "contract_version": "finance.analytics_model.v1",
            "funnels": [
                "onboarding_to_kyc",
                "collection_to_payment",
                "credit_offer_to_signature",
                "transfer_to_receipt",
                "insurance_claim_to_resolution",
            ],
            "risk_signals": [
                "stale_kyc",
                "payment_failed",
                "signature_abandoned",
                "manual_review_overdue",
                "high_value_operation",
            ],
            "dashboards": ["conversion", "collections", "identity_review", "operator_sla", "whatsapp_templates"],
        },
        "security_policy": {
            "card_data_in_chat_allowed": False,
            "identity_data_in_chat_allowed": False,
            "requires_signed_session": True,
            "requires_server_to_server_confirmation": True,
            "requires_idempotency_key": True,
            "audit_trail_required": True,
            "fallback_inside_24h_only_until_template_approved": True,
        },
        "frontend_contract": {
            "render_as": "finance_transactional_command_center",
            "show_journey_grid": True,
            "show_crm_queues": True,
            "show_security_policy": True,
            "show_analytics_model": True,
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
    audio_cache = _tts_audio_cache_observability_payload(app_config)
    qa_playbook = _qa_playbook_payload(
        tenant,
        channel_ready=channel_ready,
        template_blueprint=template_blueprint,
        webview_blueprint=webview_blueprint,
        integration_access=integration_access,
    )
    message_ux_policy = _message_ux_policy_payload(
        channel_ready=channel_ready,
        integration_access=integration_access,
        audio_cache=audio_cache,
    )
    finance_transactional = _finance_transactional_payload(
        tenant,
        template_blueprint=template_blueprint,
        webview_blueprint=webview_blueprint,
        qa_playbook=qa_playbook,
        checkout_experience=checkout_experience,
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
        "conversation_intelligence": _conversation_intelligence_payload(
            tenant,
            cfg,
            app_config,
            audio_cache=audio_cache,
        ),
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
        "finance_transactional": finance_transactional,
        "qa_playbook": qa_playbook,
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
                "transactional_finance",
                "qa_playbook",
                "message_ux_policy",
                "voice_realtime",
                "huggingface_ai",
                "enterprise_rules",
            ],
            "empty_state_behavior": "show_setup_checklist_and_safe_degradation",
        },
    }
