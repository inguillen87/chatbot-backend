import io
import logging
import mimetypes
import random
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import pandas as pd
from flask import Blueprint, jsonify, request, session, g
from flask_cors import cross_origin
from sqlalchemy import func

from database import db
from models import CatalogoItem, MunicipioTicket, PedidoConversacional, TicketComentario
from routes.catalogo import _formatear_producto
from routes.productos import _resolve_public_owner
from services.cart import _get_pyme_cart
from services.commerce_unified import _build_assisted_operator_pack, _build_operator_triage
from services.gcs_service import upload_to_gcs
from services.order_attachment_preview import build_crm_order_draft
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from services.vision_extractor import extract_table_from_file
from config import ALLOWED_ORIGINS

logger = logging.getLogger(__name__)

_ASSISTED_REQUEST_CONTRACT_VERSION = "marketplace.assisted_request.v1"
_MAX_ORDER_NOTE_BYTES = 8 * 1024 * 1024
_MAX_ORDER_TEXT_CHARS = 12000
_MAX_CATALOG_CANDIDATES_PER_ROW = 3
_MIN_CATALOG_CANDIDATE_SCORE = 0.34

_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]

pedidos_from_file_bp = Blueprint("pedidos_from_file_bp", __name__, url_prefix="/api/pedidos")

_PROMPT = """
Identificá productos y cantidades del documento. Devuelve JSON {"items": [{"sku": "...", "nombre": "...", "cantidad": 1}]}.
"""

_REQUEST_KIND_CONFIG = {
    "order_note": {
        "label": "nota de pedido",
        "crm_type": "nota_de_pedido",
        "primary_intent": "create_order_or_quote",
        "catalog_matching": True,
        "prompt": (
            'Identifica productos y cantidades del documento. Devuelve JSON '
            '{"items": [{"sku": "...", "nombre": "...", "cantidad": 1}]}. '
            "Si el texto es manuscrito, interpreta cada renglon como un posible item."
        ),
    },
    "handwritten_order": {
        "label": "nota manuscrita de pedido",
        "crm_type": "nota_de_pedido",
        "primary_intent": "create_order_or_quote",
        "catalog_matching": True,
        "prompt": (
            'Interpreta la nota manuscrita o foto de mostrador. Devuelve JSON '
            '{"items": [{"sku": "...", "nombre": "...", "cantidad": 1}]}. '
            "Prioriza productos, materiales, cantidades, unidades y cualquier observacion de entrega. "
            "Si una palabra no es segura, mantenela como nombre aproximado para revision del operador."
        ),
    },
    "quote_request": {
        "label": "pedido de cotizacion",
        "crm_type": "nota_de_pedido",
        "primary_intent": "create_quote",
        "catalog_matching": True,
        "prompt": (
            'Identifica materiales, servicios, productos y cantidades para cotizar. Devuelve JSON '
            '{"items": [{"sku": "...", "nombre": "...", "cantidad": 1}]}. '
            "Si no hay SKU, usa el nombre mas claro posible."
        ),
    },
    "receipt": {
        "label": "factura, recibo o comprobante",
        "crm_type": "solicitud_desde_archivo",
        "primary_intent": "document_review",
        "catalog_matching": False,
        "prompt": (
            'Extrae conceptos, importes, identificadores y datos utiles del comprobante. Devuelve JSON '
            '{"items": [{"sku": null, "nombre": "...", "cantidad": 1}]}.'
        ),
    },
    "tax_bill": {
        "label": "boleta de pago o impuesto",
        "crm_type": "solicitud_desde_archivo",
        "primary_intent": "tax_or_payment_support",
        "catalog_matching": False,
        "prompt": (
            'Extrae numero de cuenta, padron, periodo, vencimiento, importe, titular y concepto de la boleta. '
            'Devuelve JSON {"items": [{"sku": null, "nombre": "...", "cantidad": 1}]}. '
            "Si parece un impuesto, tasa municipal o pago de servicio, deja una descripcion clara para el equipo."
        ),
    },
    "certificate": {
        "label": "certificado o tramite",
        "crm_type": "solicitud_desde_archivo",
        "primary_intent": "certificate_or_procedure_review",
        "catalog_matching": False,
        "prompt": (
            'Extrae el tipo de certificado, datos principales y referencia principal. Devuelve JSON '
            '{"items": [{"sku": null, "nombre": "...", "cantidad": 1}]}.'
        ),
    },
    "service_request": {
        "label": "reclamo o solicitud vecinal",
        "crm_type": "solicitud_vecinal_desde_archivo",
        "primary_intent": "municipal_service_request",
        "operator_goal": "crear_ticket_o_derivar_area",
        "catalog_matching": False,
        "prompt": (
            "Interpreta reclamos, solicitudes vecinales o notas para un municipio. "
            "Extrae categoria probable, direccion, referencia, urgencia, descripcion y datos de contacto si aparecen. "
            'Devuelve JSON {"items": [{"sku": null, "nombre": "...", "cantidad": 1}]}. '
            "Cada item debe representar un problema o gestion concreta para que el equipo pueda derivarlo."
        ),
    },
    "other": {
        "label": "archivo para revisar",
        "crm_type": "solicitud_desde_archivo",
        "primary_intent": "manual_review",
        "catalog_matching": False,
        "prompt": (
            'Extrae los puntos principales del archivo. Devuelve JSON '
            '{"items": [{"sku": null, "nombre": "...", "cantidad": 1}]}.'
        ),
    },
}


def _request_kind_config(raw_kind: Optional[str]) -> tuple[str, dict]:
    normalized = (raw_kind or "order_note").strip().lower().replace("-", "_")
    if normalized not in _REQUEST_KIND_CONFIG:
        normalized = "order_note"
    return normalized, _REQUEST_KIND_CONFIG[normalized]


_REQUEST_KIND_INFERENCE_TERMS: dict[str, tuple[str, ...]] = {
    "tax_bill": (
        "boleta",
        "impuesto",
        "tasa",
        "padron",
        "padrón",
        "partida",
        "vencimiento",
        "periodo",
        "período",
        "cuenta",
        "libre deuda",
    ),
    "certificate": (
        "certificado",
        "constancia",
        "habilitacion",
        "habilitación",
        "permiso",
        "licencia",
        "tramite",
        "trámite",
        "acta",
        "partida de nacimiento",
    ),
    "receipt": (
        "factura",
        "recibo",
        "comprobante",
        "ticket",
        "transferencia",
        "pago",
    ),
    "quote_request": (
        "cotizacion",
        "cotización",
        "cotizar",
        "presupuesto",
        "presupuestar",
        "precio",
        "stock",
        "disponibilidad",
    ),
    "handwritten_order": (
        "manuscrita",
        "manuscrito",
        "papel",
        "foto de pedido",
        "foto del pedido",
        "lista escrita",
    ),
    "service_request": (
        "reclamo",
        "solicitud vecinal",
        "denuncia",
        "bache",
        "luminaria",
        "foco quemado",
        "perdida de agua",
        "perdida",
        "basura",
        "residuos",
        "poda",
        "arbolado",
        "arbol",
        "calle rota",
        "arreglo de calle",
        "cuneta",
        "cordon",
        "vereda",
        "semaforo",
        "transito",
        "limpieza",
        "riego",
    ),
}


def _normalize_inference_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").lower())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def _infer_request_kind(raw_kind: Optional[str], *, text_payload: Optional[str], filename: Optional[str]) -> tuple[str, dict[str, Any]]:
    explicit = _clean_optional_text(raw_kind)
    if explicit:
        normalized = explicit.lower().replace("-", "_")
        if normalized in _REQUEST_KIND_CONFIG:
            return normalized, {
                "method": "explicit",
                "matched_terms": [],
                "source": "document_type",
            }

    haystack = _normalize_inference_text(" ".join(part for part in [text_payload, filename] if part))
    if not haystack:
        return "order_note", {"method": "default", "matched_terms": [], "source": "empty_input"}

    for kind in ("tax_bill", "certificate", "receipt", "service_request", "quote_request", "handwritten_order"):
        matches = [
            term
            for term in _REQUEST_KIND_INFERENCE_TERMS[kind]
            if _normalize_inference_text(term) and _normalize_inference_text(term) in haystack
        ]
        if matches:
            return kind, {
                "method": "heuristic",
                "matched_terms": matches[:5],
                "source": "text_or_filename",
            }

    return "order_note", {"method": "default", "matched_terms": [], "source": "text_or_filename"}


def _rows_text_for_classification(rows: list[dict]) -> str:
    pieces: list[str] = []
    for row in rows[:25]:
        if isinstance(row, dict):
            values = [
                row.get(key)
                for key in (
                    "nombre",
                    "producto",
                    "descripcion",
                    "descripciÃ³n",
                    "detalle",
                    "concepto",
                    "tramite",
                    "trÃ¡mite",
                    "certificado",
                    "cuenta",
                    "padron",
                    "padrÃ³n",
                    "periodo",
                    "perÃ­odo",
                    "vencimiento",
                    "direccion",
                    "direcciÃ³n",
                    "ubicacion",
                    "ubicaciÃ³n",
                    "categoria",
                    "rubro",
                    "area",
                    "Ã¡rea",
                )
            ]
            row_text = " ".join(str(value).strip() for value in values if value)
            pieces.append(row_text or _row_label(row))
        elif row:
            pieces.append(str(row))
    return "\n".join(piece for piece in pieces if piece).strip()


def _maybe_reclassify_request_kind_from_extraction(
    *,
    current_kind: str,
    current_classification: dict[str, Any],
    rows: list[dict],
    text_payload: Optional[str],
    filename: Optional[str],
) -> tuple[str, dict[str, Any], dict]:
    if current_classification.get("method") != "default" or not rows:
        return current_kind, current_classification, _REQUEST_KIND_CONFIG[current_kind]

    extracted_text = _rows_text_for_classification(rows)
    if not extracted_text:
        return current_kind, current_classification, _REQUEST_KIND_CONFIG[current_kind]

    combined_text = "\n".join(part for part in (text_payload, extracted_text) if part)
    inferred_kind, inferred_classification = _infer_request_kind(
        None,
        text_payload=combined_text,
        filename=filename,
    )
    if inferred_kind == current_kind or inferred_classification.get("method") == "default":
        return current_kind, current_classification, _REQUEST_KIND_CONFIG[current_kind]

    inferred_classification = {
        **inferred_classification,
        "method": "post_extraction_heuristic",
        "source": "ocr_rows",
        "previous_kind": current_kind,
        "previous_classification": current_classification,
    }
    return inferred_kind, inferred_classification, _REQUEST_KIND_CONFIG[inferred_kind]


def _build_document_profile(
    request_kind: str,
    config: dict,
    *,
    input_type: str,
    has_file: bool,
    classification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "kind": request_kind,
        "label": config.get("label") or request_kind,
        "crm_type": config.get("crm_type") or "solicitud_desde_archivo",
        "primary_intent": config.get("primary_intent") or "manual_review",
        "catalog_matching": bool(config.get("catalog_matching")),
        "input_type": input_type,
        "input_mode": "file" if has_file else "text",
        "supports_anonymous_intake": True,
        "operator_goal": config.get("operator_goal")
        or (
            "convertir_a_pedido_o_cotizacion"
            if config.get("catalog_matching")
            else "clasificar_documento_y_responder"
        ),
        "classification": classification or {"method": "default", "matched_terms": []},
    }


def _build_review_context(
    *,
    document_profile: dict[str, Any],
    matched_count: int,
    unmatched_count: int,
    catalog_candidates: list[dict[str, Any]],
    extraction_error: Optional[str],
    contact_payload: dict[str, str],
    missing_fields: Optional[list[Any]] = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    primary_intent = str(document_profile.get("primary_intent") or "")
    catalog_matching = bool(document_profile.get("catalog_matching"))
    if extraction_error:
        reasons.append("lectura_ia_baja_confianza")
    if unmatched_count:
        if catalog_matching:
            reasons.append("items_sin_match_exacto")
        elif primary_intent == "municipal_service_request":
            reasons.append("solicitud_requiere_derivacion")
        else:
            reasons.append("documento_para_revision")
    if not matched_count and catalog_matching:
        reasons.append("catalogo_sin_match_automatico")
    if not contact_payload.get("phone") and not contact_payload.get("email"):
        reasons.append("contacto_incompleto")
    if not reasons:
        reasons.append("listo_para_confirmar")

    triage = _build_operator_triage(
        primary_intent=document_profile.get("primary_intent"),
        contact=contact_payload,
        match_summary={
            "matched": matched_count,
            "unmatched": unmatched_count,
            "detected": matched_count + unmatched_count,
            "needs_operator_review": bool(unmatched_count or extraction_error),
        },
        unmatched_items=[],
        extraction_error=extraction_error,
        missing_fields=missing_fields or [],
    )

    return {
        "contract_version": "marketplace.assisted_review.v1",
        "primary_intent": document_profile.get("primary_intent"),
        "operator_goal": document_profile.get("operator_goal"),
        "review_reasons": reasons,
        "priority": triage.get("priority"),
        "priority_reason": triage.get("priority_reason"),
        "priority_reason_label": triage.get("priority_reason_label"),
        "sla_hint": triage.get("sla_hint"),
        "operator_queue": triage.get("operator_queue"),
        "operator_queue_label": triage.get("operator_queue_label"),
        "primary_missing_field": triage.get("primary_missing_field"),
        "missing_fields": triage.get("missing_fields"),
        "catalog_matching_enabled": bool(document_profile.get("catalog_matching")),
        "catalog_candidate_groups": len(catalog_candidates),
        "recommended_channels": ["whatsapp", "email", "phone", "crm"],
        "summary": {
            "matched": matched_count,
            "unmatched": unmatched_count,
            "candidate_groups": len(catalog_candidates),
            "has_contact": bool(contact_payload.get("phone") or contact_payload.get("email")),
        },
    }


def _pick_first_row_value(rows: list[dict], keys: list[str]) -> Optional[str]:
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _first_text(row, keys)
        if value:
            return value
    return None


def _build_structured_extraction(
    *,
    document_profile: dict[str, Any],
    rows: list[dict],
    text_payload: Optional[str],
    contact_payload: dict[str, str],
) -> dict[str, Any]:
    primary_intent = str(document_profile.get("primary_intent") or "")
    catalog_matching = bool(document_profile.get("catalog_matching"))
    normalized_text = _normalize_inference_text(text_payload or " ".join(_row_label(row) for row in rows))

    payload: dict[str, Any] = {
        "contract_version": "marketplace.structured_extraction.v1",
        "primary_intent": primary_intent,
        "catalog_matching": catalog_matching,
        "confidence": "needs_review",
        "fields": {},
        "missing_fields": [],
        "source": "llm_or_text_rows",
    }

    if primary_intent == "municipal_service_request":
        category = _pick_first_row_value(rows, ["categoria", "categoría", "rubro", "area", "área", "tipo"]) or _infer_service_category(normalized_text)
        address = (
            _pick_first_row_value(rows, ["direccion", "dirección", "ubicacion", "ubicación", "domicilio", "referencia"])
            or _infer_service_address_from_text(text_payload, rows)
        )
        description = _pick_first_row_value(rows, ["descripcion", "descripción", "detalle", "reclamo", "solicitud", "nombre", "concepto"])
        urgency = _pick_first_row_value(rows, ["urgencia", "prioridad"]) or _infer_service_urgency(normalized_text)
        fields = {
            "categoria_probable": category,
            "direccion": address,
            "descripcion": description,
            "urgencia": urgency,
            "contacto": contact_payload or None,
        }
        required = ["categoria_probable", "direccion", "descripcion"]
    elif primary_intent == "tax_or_payment_support":
        fields = {
            "cuenta_o_padron": _pick_first_row_value(rows, ["cuenta", "padron", "padrón", "partida", "nro_cuenta", "numero_cuenta"]),
            "periodo": _pick_first_row_value(rows, ["periodo", "período", "mes", "anio", "año"]),
            "vencimiento": _pick_first_row_value(rows, ["vencimiento", "vence", "fecha_vencimiento"]),
            "importe": _pick_first_row_value(rows, ["importe", "monto", "total", "saldo"]),
            "concepto": _pick_first_row_value(rows, ["concepto", "detalle", "descripcion", "descripción", "nombre"]),
            "titular": _pick_first_row_value(rows, ["titular", "nombre", "contribuyente"]),
            "contacto": contact_payload or None,
        }
        required = ["cuenta_o_padron", "periodo", "vencimiento", "concepto"]
    elif primary_intent == "certificate_or_procedure_review":
        fields = {
            "tipo_tramite": _pick_first_row_value(rows, ["tipo", "tramite", "trámite", "certificado", "concepto", "nombre"]),
            "titular": _pick_first_row_value(rows, ["titular", "nombre", "solicitante"]),
            "identificador": _pick_first_row_value(rows, ["dni", "cuit", "id", "expediente", "numero", "número"]),
            "observaciones": _pick_first_row_value(rows, ["observaciones", "detalle", "descripcion", "descripción"]),
            "contacto": contact_payload or None,
        }
        required = ["tipo_tramite", "titular"]
    else:
        fields = {
            "resumen": _pick_first_row_value(rows, ["resumen", "concepto", "detalle", "descripcion", "descripción", "nombre"]),
            "contacto": contact_payload or None,
        }
        required = [] if catalog_matching else ["resumen"]

    payload["fields"] = {key: value for key, value in fields.items() if value}
    payload["missing_fields"] = [key for key in required if not fields.get(key)]
    if not payload["missing_fields"] and payload["fields"]:
        payload["confidence"] = "ready_for_operator"
    return payload


def _infer_service_category(normalized_text: str) -> Optional[str]:
    category_terms = [
        ("Luminaria", ("luminaria", "foco", "luz", "alumbrado")),
        ("Arbolado", ("arbol", "arbolado", "poda", "rama")),
        ("Perdida de agua", ("perdida de agua", "agua", "cano", "caño")),
        ("Limpieza", ("basura", "residuos", "limpieza", "riego")),
        ("Arreglo de calle", ("bache", "calle rota", "arreglo de calle", "cuneta", "cordon", "vereda")),
        ("Transito", ("semaforo", "transito", "estacionamiento")),
    ]
    padded = f" {normalized_text} "
    for label, terms in category_terms:
        if any(f" {_normalize_inference_text(term)} " in padded for term in terms):
            return label
    return None


def _infer_service_urgency(normalized_text: str) -> str:
    urgent_terms = ("peligro", "riesgo", "urgente", "electrico", "inundacion", "perdida de gas", "accidente")
    padded = f" {normalized_text} "
    if any(f" {_normalize_inference_text(term)} " in padded for term in urgent_terms):
        return "alta"
    return "normal"


def _trim_inferred_address(value: str) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;-")
    if not cleaned:
        return None
    stop_patterns = [
        r"\b(de noche|queda|esta|estan|hay|no funciona|no anda|por favor|urgente|gracias)\b",
        r"\b(necesito|solicito|pido|reclamo)\b",
    ]
    for pattern in stop_patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match and match.start() >= 8:
            cleaned = cleaned[: match.start()].strip(" .,:;-")
            break
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rsplit(" ", 1)[0].strip(" .,:;-")
    return cleaned or None


def _infer_service_address_from_text(text_payload: Optional[str], rows: list[dict]) -> Optional[str]:
    pieces: list[str] = []
    if text_payload:
        pieces.append(str(text_payload))
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        for key in ("descripcion", "descripción", "detalle", "reclamo", "solicitud", "nombre", "concepto"):
            value = row.get(key)
            if value:
                pieces.append(str(value))
    haystack = "\n".join(pieces)
    if not haystack:
        return None

    contextual_patterns = [
        r"(?:direccion|dirección|ubicacion|ubicación|domicilio)\s*[:\-]?\s*(?P<address>[^.\n;]{6,120})",
        r"(?:\ben\b|\bsobre\b|\bcalle\b)\s+(?P<address>[A-Za-z0-9ÁÉÍÓÚÑáéíóúñ .,'-]{6,120})",
    ]
    for pattern in contextual_patterns:
        match = re.search(pattern, haystack, flags=re.IGNORECASE)
        if match:
            address = _trim_inferred_address(match.group("address"))
            if address:
                return address

    direct_match = re.search(
        r"\b(?P<address>[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ .'-]{2,45}\s+\d{1,5}"
        r"(?:\s+(?:esq\.?|esquina|y)\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ .'-]{2,45})?)\b",
        haystack,
    )
    if direct_match:
        return _trim_inferred_address(direct_match.group("address"))
    return None


def _build_crm_handoff_payload(
    *,
    document_profile: dict[str, Any],
    structured_extraction: dict[str, Any],
    source_payload: dict[str, Any],
    contact_payload: dict[str, str],
) -> dict[str, Any]:
    primary_intent = str(document_profile.get("primary_intent") or "")
    fields = structured_extraction.get("fields") if isinstance(structured_extraction.get("fields"), dict) else {}
    base = {
        "contract_version": "marketplace.crm_handoff.v1",
        "primary_intent": primary_intent,
        "operator_goal": document_profile.get("operator_goal"),
        "contact": contact_payload,
        "source": {
            "channel": source_payload.get("channel"),
            "archivo_url": source_payload.get("archivo_url"),
            "archivo_nombre": source_payload.get("archivo_nombre"),
            "original_filename": source_payload.get("original_filename"),
            "mime_type": source_payload.get("mime_type"),
            "file_size_bytes": source_payload.get("file_size_bytes"),
            "text_preview": source_payload.get("text_preview"),
        },
        "structured_extraction": structured_extraction,
    }
    if primary_intent == "municipal_service_request":
        base.update(
            {
                "target_module": "municipal_claims",
                "recommended_record": "tenant_ticket",
                "draft_ticket": {
                    "categoria": fields.get("categoria_probable"),
                    "direccion": fields.get("direccion"),
                    "descripcion": fields.get("descripcion"),
                    "prioridad": fields.get("urgencia") or "normal",
                    "canal_ingreso": source_payload.get("channel") or "marketplace",
                    "estado": "nuevo",
                },
            }
        )
    elif primary_intent in {"tax_or_payment_support", "certificate_or_procedure_review", "document_review", "manual_review"}:
        base.update(
            {
                "target_module": "document_requests",
                "recommended_record": "crm_task",
                "draft_task": {
                    "tipo": primary_intent,
                    "campos": fields,
                    "estado": "pendiente_revision",
                },
            }
        )
    else:
        base.update({"target_module": "orders", "recommended_record": "assisted_order"})
    return base


def _build_operator_intake_summary(
    *,
    document_profile: dict[str, Any],
    request_kind_label: str,
    source_payload: dict[str, Any],
    contact_payload: dict[str, str],
    match_summary: dict[str, Any],
    crm_handoff: dict[str, Any],
    enriched_items: list[dict[str, Any]],
    unmatched_labels: list[str],
    public_follow_up: dict[str, Any],
) -> dict[str, Any]:
    primary_intent = str(document_profile.get("primary_intent") or "")
    target_module = str(crm_handoff.get("target_module") or "orders")
    has_contact = bool(contact_payload.get("phone") or contact_payload.get("email"))
    needs_review = bool(match_summary.get("needs_operator_review"))
    structured_extraction = (
        crm_handoff.get("structured_extraction")
        if isinstance(crm_handoff.get("structured_extraction"), dict)
        else {}
    )
    missing_fields = (
        structured_extraction.get("missing_fields")
        if isinstance(structured_extraction.get("missing_fields"), list)
        else []
    )
    triage = _build_operator_triage(
        primary_intent=primary_intent,
        target_module=target_module,
        contact=contact_payload,
        match_summary=match_summary,
        unmatched_items=unmatched_labels,
        extraction_error=source_payload.get("extraction_error"),
        missing_fields=missing_fields,
    )
    preview: list[str] = []
    for item in enriched_items[:4]:
        label = _clean_optional_text(
            item.get("nombre") or item.get("name") or item.get("title") or item.get("sku")
        )
        if label:
            quantity = item.get("cantidad") or item.get("quantity")
            preview.append(f"{quantity} {label}" if quantity else label)
    for label in unmatched_labels:
        if label and label not in preview:
            preview.append(label)
        if len(preview) >= 6:
            break

    if primary_intent == "municipal_service_request":
        objective = "Validar categoria, direccion, urgencia y derivar area responsable."
        recommended_next_step = "derivar_area_y_responder"
    elif target_module == "document_requests":
        objective = "Validar datos del documento y responder proximo paso del tramite."
        recommended_next_step = "revisar_documento_y_responder"
    else:
        objective = "Confirmar stock, precio, alternativas y convertir la nota en pedido o cotizacion."
        recommended_next_step = "confirmar_stock_precio_y_responder"
    if needs_review:
        recommended_next_step = "resolver_faltantes_y_responder"
    if not has_contact:
        recommended_next_step = "pedir_contacto_y_responder"

    tracking = public_follow_up.get("tracking") if isinstance(public_follow_up.get("tracking"), dict) else {}
    return {
        "contract_version": "marketplace.operator_intake_summary.v1",
        "title": f"{request_kind_label.capitalize()} desde {source_payload.get('channel') or 'marketplace'}",
        "objective": objective,
        "primary_intent": primary_intent,
        "target_module": target_module,
        "recommended_record": crm_handoff.get("recommended_record"),
        "recommended_next_step": recommended_next_step,
        "needs_operator_review": needs_review,
        "priority": triage.get("priority"),
        "priority_reason": triage.get("priority_reason"),
        "priority_reason_label": triage.get("priority_reason_label"),
        "sla_hint": triage.get("sla_hint"),
        "operator_queue": triage.get("operator_queue"),
        "operator_queue_label": triage.get("operator_queue_label"),
        "primary_missing_field": triage.get("primary_missing_field"),
        "missing_fields": triage.get("missing_fields"),
        "contact_state": "available" if has_contact else "missing",
        "contact_channels": [
            channel
            for channel in ("phone", "email")
            if contact_payload.get(channel)
        ],
        "input": {
            "mode": document_profile.get("input_mode"),
            "type": source_payload.get("input_type"),
            "channel": source_payload.get("channel"),
            "file_name": source_payload.get("archivo_nombre"),
            "original_filename": source_payload.get("original_filename"),
            "mime_type": source_payload.get("mime_type"),
            "file_size_bytes": source_payload.get("file_size_bytes"),
            "has_file": bool(source_payload.get("archivo_url")),
            "has_text": bool(source_payload.get("text_preview")),
        },
        "detected_preview": preview,
        "match_summary": match_summary,
        "follow_up": {
            "kind": tracking.get("kind"),
            "code": tracking.get("code"),
            "path": tracking.get("path"),
        },
    }


def _build_customer_next_steps(
    *,
    document_profile: dict[str, Any],
    request_kind_label: str,
    matched_count: int,
    unmatched_count: int,
    extraction_error: Optional[str],
) -> list[dict[str, Any]]:
    primary_intent = str(document_profile.get("primary_intent") or "")
    catalog_matching = bool(document_profile.get("catalog_matching"))
    is_service_request = primary_intent == "municipal_service_request"
    is_document_review = not catalog_matching and not is_service_request

    steps: list[dict[str, Any]] = [
        {
            "id": "received",
            "label": "Solicitud recibida",
            "description": f"Chatboc registro tu {request_kind_label} y la dejo asociada al espacio correspondiente.",
            "status": "done",
        }
    ]
    if matched_count and catalog_matching:
        steps.append(
            {
                "id": "catalog_draft",
                "label": "Borrador con catalogo",
                "description": f"{matched_count} renglon(es) ya quedaron vinculados a productos disponibles.",
                "status": "done",
            }
        )
    if unmatched_count or extraction_error:
        if is_service_request:
            review_step = {
                "id": "municipal_triage",
                "label": "Derivacion municipal",
                "description": "El equipo valida categoria, direccion, urgencia y area responsable antes de responder.",
                "status": "pending",
            }
        elif is_document_review:
            review_step = {
                "id": "document_review",
                "label": "Revision documental",
                "description": "Un operador valida datos, periodo, cuenta, tramite o comprobante antes de responder.",
                "status": "pending",
            }
        else:
            review_step = {
                "id": "human_review",
                "label": "Revision del equipo",
                "description": "Un operador revisa faltantes, lectura del archivo, stock, precio o datos de entrega.",
                "status": "pending",
            }
        steps.append(
            review_step
        )
    reply_description = "El equipo puede continuar por WhatsApp, chat, email o telefono con el proximo paso."
    if is_service_request:
        reply_description = "El vecino recibe estado, area responsable y proximo paso por el canal elegido."
    elif is_document_review:
        reply_description = "El solicitante recibe validacion, observaciones y proximo paso del tramite."
    steps.append(
        {
            "id": "reply",
            "label": "Respuesta por canal",
            "description": reply_description,
            "status": "pending",
        }
    )
    return steps


def _build_intake_experience(
    *,
    document_profile: dict[str, Any],
    request_kind_label: str,
    matched_count: int,
    unmatched_count: int,
    detected_count: int,
    extraction_error: Optional[str],
    contact_payload: dict[str, str],
) -> dict[str, Any]:
    catalog_matching = bool(document_profile.get("catalog_matching"))
    primary_intent = str(document_profile.get("primary_intent") or "")
    is_service_request = primary_intent == "municipal_service_request"
    is_document_review = not catalog_matching and not is_service_request
    needs_review = unmatched_count > 0 or matched_count == 0 or bool(extraction_error)
    customer_has_contact = bool(contact_payload.get("phone") or contact_payload.get("email"))

    if catalog_matching:
        analysis_step = {
            "id": "catalog_match",
            "label": "Cruce con catalogo",
            "description": "Productos compatibles, alternativas y renglones pendientes para revisar.",
            "status": "done" if matched_count else "pending_review",
        }
        handoff_description = "Pedido o cotizacion queda listo para confirmar stock, precio y proximo paso."
        recommended_next_action = "revisar_y_responder" if needs_review else "confirmar_stock_precio_y_enviar"
        title = "Pedido asistido por foto, papel o texto"
    elif is_service_request:
        analysis_step = {
            "id": "municipal_triage",
            "label": "Clasificacion municipal",
            "description": "Categoria, direccion, referencia y urgencia quedan preparados para derivacion.",
            "status": "done" if detected_count else "pending_review",
        }
        handoff_description = "Reclamo o solicitud queda listo para validar area, ubicacion y respuesta al vecino."
        recommended_next_action = "derivar_area_y_responder"
        title = "Reclamo asistido por foto, papel o texto"
    else:
        analysis_step = {
            "id": "document_review",
            "label": "Revision documental",
            "description": "Datos utiles del comprobante, boleta o certificado quedan preparados para revisar.",
            "status": "done" if detected_count else "pending_review",
        }
        handoff_description = "Documento queda listo para validar datos y responder el proximo paso."
        recommended_next_action = "revisar_y_responder"
        title = "Documento asistido por foto, papel o texto"

    pipeline = [
        {
            "id": "capture",
            "label": "Archivo o texto recibido",
            "description": "Entrada anonima desde marketplace, WhatsApp o widget.",
            "status": "done",
        },
        {
            "id": "ai_parse",
            "label": "Lectura automatica",
            "description": "Archivo o texto convertido a datos ordenados para revisar.",
            "status": "warning" if extraction_error else ("done" if detected_count else "pending_review"),
        },
        analysis_step,
        {
            "id": "crm_handoff",
            "label": "Listo para el equipo",
            "description": handoff_description,
            "status": "pending_review" if needs_review else "ready",
        },
    ]

    examples = ["Foto de una lista escrita a mano", "Pedido pegado desde WhatsApp", "PDF, Excel, comprobante o boleta"]
    if catalog_matching:
        examples.insert(1, "Lista de materiales, ferreteria, bebidas o supermercado")
    elif is_service_request:
        examples = [
            "Foto de un reclamo escrito",
            "Direccion o referencia pegada desde WhatsApp",
            "Solicitud por luminaria, bache, arbolado o limpieza",
        ]
    elif is_document_review:
        examples = [
            "Boleta o comprobante",
            "Certificado o tramite escaneado",
            "Datos pegados desde WhatsApp",
        ]

    contextual_capability = {
        "id": "catalog_candidate_matching",
        "label": "Candidatos de catalogo",
        "description": "Propone articulos similares cuando no hay match exacto.",
        "status": "enabled" if catalog_matching else "available_for_catalogs",
    }
    if is_service_request:
        contextual_capability = {
            "id": "municipal_triage",
            "label": "Derivacion municipal",
            "description": "Ordena categoria, ubicacion, urgencia y area responsable para el equipo.",
            "status": "enabled",
        }
    elif is_document_review:
        contextual_capability = {
            "id": "document_data_extraction",
            "label": "Lectura documental",
            "description": "Extrae concepto, cuenta, periodo, vencimiento, titular o datos del tramite.",
            "status": "enabled",
        }

    return {
        "contract_version": "marketplace.assisted_intake_experience.v1",
        "render_as": "anonymous_assisted_marketplace_intake",
        "title": title,
        "summary": (
            f"Chatboc recibio tu {request_kind_label}, separa datos utiles y lo deja listo "
            "para que el equipo responda sin exigir registro previo."
        ),
        "anonymous_intake": True,
        "customer_has_contact": customer_has_contact,
        "catalog_matching": catalog_matching,
        "needs_operator_review": needs_review,
        "input_examples": examples,
        "capabilities": [
            {
                "id": "handwritten_note_ocr",
                "label": "Notas manuscritas",
                "description": "Acepta fotos de papel, mostrador o listas poco estructuradas.",
                "status": "enabled",
            },
            contextual_capability,
            {
                "id": "crm_operator_pack",
                "label": "Resumen para el equipo",
                "description": "Incluye resumen, faltantes, proximo paso y canales de contacto.",
                "status": "enabled",
            },
            {
                "id": "omnichannel_reply",
                "label": "Respuesta omnicanal",
                "description": "Continuidad por WhatsApp, chat, email o telefono.",
                "status": "enabled",
            },
        ],
        "pipeline": pipeline,
        "customer_prompts": [
            {"id": "upload_handwritten", "label": "Subir foto de una nota", "document_type": "handwritten_order"},
            {"id": "paste_order", "label": "Pegar pedido como texto", "document_type": "order_note"},
            {"id": "send_bill", "label": "Adjuntar boleta o comprobante", "document_type": "tax_bill"},
        ],
        "crm_handoff": {
            "label": "Solicitud lista para seguimiento" if not catalog_matching else "Pedido listo para seguimiento",
            "recommended_next_action": recommended_next_action,
            "channels": ["whatsapp", "chat_widget", "email", "phone"],
        },
        "frontend_contract": {
            "render_as": "marketplace_assisted_intake",
            "primary_cta": "Subir foto o papel",
            "secondary_cta": "Escribir pedido",
            "show_on_empty_catalog": True,
        },
    }


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _form_or_json_value(json_payload: Optional[dict], *keys: str) -> Optional[str]:
    for key in keys:
        value = request.form.get(key)
        if value:
            return value
    if isinstance(json_payload, dict):
        for key in keys:
            value = json_payload.get(key)
            if value:
                return str(value)
    return None


def _json_error(status_code: int, code: str, message: str):
    response = jsonify({"codigo": code, "mensaje": message})
    response.status_code = status_code
    return response


def _reset_file_pointer(file_storage) -> None:
    try:
        file_storage.stream.seek(0)
    except Exception:  # noqa: BLE001
        try:
            file_storage.seek(0)
        except Exception:  # noqa: BLE001
            logger.debug("No se pudo rebobinar archivo subido antes de guardarlo")


def _row_label(row: object) -> str:
    if isinstance(row, dict):
        sku = row.get("sku")
        nombre = (
            row.get("nombre")
            or row.get("producto")
            or row.get("descripcion")
            or row.get("nombre_producto_ocr")
            or row.get("detalle")
            or row.get("item")
        )
        cantidad = row.get("cantidad") or row.get("qty") or row.get("unidades")
        pieces = [str(part).strip() for part in (cantidad, nombre, sku) if part]
        return " ".join(pieces) or str(row)
    return str(row)


def _first_text(row: dict, keys: List[str]) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _coerce_positive_quantity(value: object) -> tuple[int, Optional[str]]:
    if value is None or value == "":
        return 1, None
    if isinstance(value, (int, float)):
        parsed = int(value)
    else:
        match = re.search(r"\d+(?:[\.,]\d+)?", str(value))
        if not match:
            return 1, f"Cantidad no numerica: {value}"
        parsed = int(float(match.group(0).replace(",", ".")))
    if parsed <= 0:
        return 1, f"Cantidad no positiva: {value}"
    return parsed, None


def _normalize_extracted_row(row: object) -> tuple[Optional[dict], Optional[dict]]:
    if not isinstance(row, dict):
        label = _row_label(row)
        return {"nombre": label, "cantidad": 1}, {"row": label, "code": "row_not_structured"}

    sku = _first_text(row, ["sku", "codigo", "código", "code", "codigo_producto", "product_code"])
    nombre = _first_text(
        row,
        [
            "nombre",
            "producto",
            "descripcion",
            "descripción",
            "detalle",
            "item",
            "articulo",
            "artículo",
            "nombre_producto",
            "nombre_producto_ocr",
            "concepto",
        ],
    )
    cantidad, quantity_warning = _coerce_positive_quantity(
        row.get("cantidad") or row.get("qty") or row.get("quantity") or row.get("unidades")
    )
    if not sku and not nombre:
        return None, {"row": _row_label(row), "code": "missing_product_name"}

    normalized = {"sku": sku, "nombre": nombre, "cantidad": cantidad}
    warning = {"row": _row_label(row), "code": "quantity_normalized", "detail": quantity_warning} if quantity_warning else None
    return normalized, warning


def _normalize_match_text(value: object) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value).lower())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def _match_tokens(value: object) -> set[str]:
    return {token for token in _normalize_match_text(value).split() if len(token) >= 2}


def _safe_float(value: object) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _catalog_candidate_confidence(score: float) -> str:
    if score >= 0.78:
        return "high"
    if score >= 0.52:
        return "medium"
    return "low"


def _catalog_item_candidate_payload(item: CatalogoItem) -> dict:
    return {
        "catalogo_item_id": item.id,
        "product_id": item.id,
        "sku": item.sku,
        "nombre": item.nombre,
        "name": item.nombre,
        "precio": item.precio,
        "price": _safe_float(item.precio),
        "unidad": item.unidad,
        "imagen_url": item.imagen_url,
        "modalidad": item.modalidad,
        "disponible": bool(getattr(item, "disponible", True)),
    }


def _score_catalog_candidate(row: dict, item: CatalogoItem) -> tuple[float, str]:
    row_sku = _normalize_match_text(row.get("sku"))
    row_name = _normalize_match_text(row.get("nombre"))
    row_query = " ".join(part for part in [row_sku, row_name] if part).strip()
    if not row_query:
        return 0.0, "Sin texto suficiente para comparar"

    item_sku = _normalize_match_text(item.sku)
    item_name = _normalize_match_text(item.nombre)
    item_text = _normalize_match_text(
        " ".join(
            str(part)
            for part in [item.sku, item.nombre, item.marca, item.categoria, item.descripcion]
            if part
        )
    )

    scores: list[float] = []
    reasons: list[str] = []

    if row_sku and item_sku and (row_sku in item_sku or item_sku in row_sku):
        scores.append(0.88)
        reasons.append("SKU parcial compatible")

    if row_name and item_name:
        ratio = SequenceMatcher(None, row_name, item_name).ratio()
        if ratio >= 0.45:
            scores.append(min(0.86, ratio))
            reasons.append("Nombre similar al texto detectado")
        if row_name in item_name or item_name in row_name:
            scores.append(0.9)
            reasons.append("Nombre contenido parcialmente")

    row_tokens = _match_tokens(row_query)
    item_tokens = _match_tokens(item_text)
    overlap = row_tokens & item_tokens
    if row_tokens and item_tokens and overlap:
        coverage = len(overlap) / len(row_tokens)
        jaccard = len(overlap) / len(row_tokens | item_tokens)
        scores.append(min(0.92, coverage * 0.72 + jaccard * 0.28))
        shared = ", ".join(sorted(overlap)[:4])
        reasons.append(f"Comparte terminos clave: {shared}")

    score = max(scores) if scores else 0.0
    reason = reasons[0] if reasons else "Producto del catalogo con similitud baja"
    return round(score, 2), reason


def _catalog_candidates_for_row(row: dict, catalog_items: List[CatalogoItem]) -> List[dict]:
    candidates = []
    for item in catalog_items:
        score, reason = _score_catalog_candidate(row, item)
        if score < _MIN_CATALOG_CANDIDATE_SCORE:
            continue
        candidates.append(
            {
                **_catalog_item_candidate_payload(item),
                "score": score,
                "confidence": _catalog_candidate_confidence(score),
                "reason": reason,
            }
        )
    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    return candidates[:_MAX_CATALOG_CANDIDATES_PER_ROW]


def _unmatched_catalog_candidates_payload(rows: List[dict]) -> List[dict]:
    payload = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_without_candidates = {
            key: value
            for key, value in row.items()
            if key not in {"catalog_candidates", "candidates"}
        }
        payload.append(
            {
                "item": _row_label(row_without_candidates),
                "row": row_without_candidates,
                "candidates": row.get("catalog_candidates") or [],
            }
        )
    return payload


def _whatsapp_handoff_url(phone: Optional[str], text: str) -> str:
    encoded_text = quote_plus(text)
    digits = re.sub(r"\D+", "", str(phone or ""))
    if digits:
        return f"https://wa.me/{digits}?text={encoded_text}"
    return f"https://wa.me/?text={encoded_text}"


def _build_public_follow_up(
    *,
    pedido_id: int,
    tenant_slug: Optional[str],
    request_kind_label: str,
    customer_message: str,
    linked_claim: Optional[dict[str, Any]] = None,
    whatsapp_phone: Optional[str] = None,
) -> dict[str, Any]:
    if linked_claim:
        raw_code = str(linked_claim.get("nro_ticket") or "").strip()
        display_code = raw_code if raw_code.upper().startswith(("M-", "S-")) else f"M-{raw_code}"
        pin = str(linked_claim.get("consulta_pin") or "").strip()
        tracking_path = f"/tracking/claim/{quote_plus(raw_code)}"
        tracking_api = f"/api/public/tracking/experience?kind=claim&code={quote_plus(display_code)}"
        if pin:
            encoded_pin = quote_plus(pin)
            tracking_path = f"{tracking_path}?pin={encoded_pin}"
            tracking_api = f"{tracking_api}&pin={encoded_pin}"
        whatsapp_text = (
            f"Hola, quiero continuar mi {request_kind_label}. "
            f"Reclamo {display_code}. PIN {pin}. {customer_message}"
        )
        whatsapp_url = _whatsapp_handoff_url(whatsapp_phone, whatsapp_text)
        return {
            "contract_version": "marketplace.assisted_followup.v1",
            "tracking": {
                "kind": "claim",
                "code": display_code,
                "raw_code": raw_code,
                "pin": pin,
                "ticket_id": linked_claim.get("id"),
                "path": tracking_path,
                "api_endpoint": tracking_api,
                "label": "Seguimiento de reclamo",
            },
            "channels": [
                {
                    "id": "tracking_page",
                    "label": "Ver reclamo",
                    "type": "link",
                    "href": tracking_path,
                    "description": "Abri el estado del reclamo y deja mensajes si hace falta.",
                },
                {
                    "id": "whatsapp_handoff",
                    "label": "Continuar por WhatsApp",
                    "type": "link",
                    "href": whatsapp_url,
                    "description": "Abri WhatsApp con el numero de reclamo y PIN preparados.",
                },
            ],
        }

    tracking_code = f"pc-{pedido_id}"
    tracking_path = f"/tracking/order/{quote_plus(tracking_code)}"
    tracking_api = f"/api/public/tracking/experience?kind=order&code={quote_plus(tracking_code)}"
    if tenant_slug:
        encoded_tenant = quote_plus(tenant_slug)
        tracking_path = f"{tracking_path}?tenant_slug={encoded_tenant}"
        tracking_api = f"{tracking_api}&tenant_slug={encoded_tenant}"

    whatsapp_text = (
        f"Hola, quiero continuar mi {request_kind_label}. "
        f"Referencia {tracking_code}. {customer_message}"
    )
    whatsapp_url = _whatsapp_handoff_url(whatsapp_phone, whatsapp_text)
    return {
        "contract_version": "marketplace.assisted_followup.v1",
        "tracking": {
            "kind": "order",
            "code": tracking_code,
            "path": tracking_path,
            "api_endpoint": tracking_api,
            "label": "Seguimiento de solicitud",
        },
        "channels": [
            {
                "id": "tracking_page",
                "label": "Ver seguimiento",
                "type": "link",
                "href": tracking_path,
                "description": "Abri el estado de la solicitud y deja mensajes si hace falta.",
            },
            {
                "id": "whatsapp_handoff",
                "label": "Continuar por WhatsApp",
                "type": "link",
                "href": whatsapp_url,
                "description": "Abri WhatsApp con la referencia y el resumen ya preparados.",
            },
        ],
    }


def _generate_marketplace_claim_number() -> str:
    for _ in range(12):
        value = str(random.randint(100000, 999999))
        if not MunicipioTicket.query.filter_by(nro_ticket=value).first():
            return value
    return str(random.randint(1000000, 9999999))


def _materialize_municipal_claim_from_handoff(
    *,
    pedido: PedidoConversacional,
    tenant,
    owner,
    user,
    document_profile: dict[str, Any],
    structured_extraction: dict[str, Any],
    crm_handoff: dict[str, Any],
    source_payload: dict[str, Any],
    contact_payload: dict[str, str],
    rows: list[dict],
    text_payload: Optional[str],
    upload_meta: dict[str, Any],
    origen: str,
) -> Optional[dict[str, Any]]:
    if str(document_profile.get("primary_intent") or "") != "municipal_service_request":
        return None
    if crm_handoff.get("target_module") != "municipal_claims":
        return None

    fields = structured_extraction.get("fields") if isinstance(structured_extraction.get("fields"), dict) else {}
    draft_ticket = crm_handoff.get("draft_ticket") if isinstance(crm_handoff.get("draft_ticket"), dict) else {}
    categoria = (
        draft_ticket.get("categoria")
        or fields.get("categoria_probable")
        or _infer_service_category(_normalize_inference_text(text_payload or _rows_text_for_classification(rows)))
        or "General"
    )
    descripcion = (
        draft_ticket.get("descripcion")
        or fields.get("descripcion")
        or _rows_text_for_classification(rows)
        or source_payload.get("text_preview")
        or "Solicitud municipal recibida desde marketplace asistido"
    )
    direccion = draft_ticket.get("direccion") or fields.get("direccion") or contact_payload.get("notes")
    prioridad = draft_ticket.get("prioridad") or fields.get("urgencia") or "normal"
    vecino_nombre = contact_payload.get("name") or "Vecino/a"
    telefono = contact_payload.get("phone")
    email = contact_payload.get("email")
    municipio_id = getattr(tenant, "municipio_id", None) or getattr(owner, "id", None)
    actor_id = getattr(user, "id", None)
    anon_id = source_payload.get("anon_id") or getattr(pedido, "anon_id", None)

    is_image_source = str(source_payload.get("input_type") or "").lower() in {"png", "jpg", "jpeg", "webp"}
    ticket = MunicipioTicket(
        tenant_id=getattr(tenant, "id", None),
        municipio_id=municipio_id,
        user_id=actor_id,
        anon_id=anon_id,
        nro_ticket=_generate_marketplace_claim_number(),
        asunto=f"Reclamo asistido: {categoria}",
        categoria=categoria,
        pregunta=descripcion,
        detalles=descripcion,
        direccion=direccion,
        nombre_vecino=vecino_nombre,
        telefono_vecino=telefono,
        email_vecino=email,
        foto_url_directa=upload_meta.get("public_url") if is_image_source else None,
        canal_ingreso="marketplace_asistido",
        contacto_seguimiento=telefono or email,
        estado="nuevo",
    )
    db.session.add(ticket)
    db.session.flush()

    comentario = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=(
            "Solicitud creada automaticamente desde marketplace asistido. "
            f"Referencia intake pc-{pedido.id}. La lectura sugirio categoria '{categoria}'"
            + (f" y direccion '{direccion}'." if direccion else ".")
        ),
        user_id=actor_id or municipio_id,
        es_admin=False,
        origen="marketplace_asistido",
    )
    db.session.add(comentario)

    linked_record = {
        "kind": "municipio_ticket",
        "target_module": "municipal_claims",
        "id": ticket.id,
        "ticket_id": ticket.id,
        "ticket_type": "municipio",
        "nro_ticket": str(ticket.nro_ticket),
        "display_code": f"M-{ticket.nro_ticket}",
        "consulta_pin": ticket.consulta_pin,
        "status": ticket.estado,
        "category": ticket.categoria,
        "address": ticket.direccion,
        "admin_thread_binding": "municipio_ticket_id",
    }
    crm_handoff["materialized_record"] = linked_record
    crm_handoff["recommended_record"] = "municipio_ticket"
    crm_handoff["draft_ticket"] = {
        **draft_ticket,
        "categoria": categoria,
        "direccion": direccion,
        "descripcion": descripcion,
        "prioridad": prioridad,
        "estado": ticket.estado,
        "ticket_id": ticket.id,
        "nro_ticket": str(ticket.nro_ticket),
        "consulta_pin": ticket.consulta_pin,
    }
    return linked_record


def _build_next_actions(
    *,
    pedido_id: int,
    tenant_slug: Optional[str],
    document_profile: dict[str, Any],
    matched_count: int,
    unmatched_count: int,
) -> List[dict]:
    tenant_path = f"/t/{tenant_slug}" if tenant_slug else ""
    primary_intent = str(document_profile.get("primary_intent") or "")
    catalog_matching = bool(document_profile.get("catalog_matching"))
    is_service_request = primary_intent == "municipal_service_request"
    is_document_review = not catalog_matching and not is_service_request

    operator_description = "El equipo revisa la nota, corrige articulos y responde por WhatsApp, email o telefono."
    complete_description = "Un operador puede confirmar faltantes, precios, stock y datos de entrega."
    review_label = "Revisar articulos no encontrados"
    review_description = "Hay renglones que no matchearon con el catalogo y requieren revision humana."
    if is_service_request:
        operator_description = "El equipo valida categoria, direccion, urgencia y area antes de responder al vecino."
        complete_description = "Un operador puede derivar el reclamo, pedir datos faltantes y dejar seguimiento publico."
        review_label = "Validar datos del reclamo"
        review_description = "Hay direccion, categoria, descripcion o contacto para revisar antes de derivar el area."
    elif is_document_review:
        operator_description = "El equipo revisa el documento, valida datos clave y responde el proximo paso."
        complete_description = "Un operador puede pedir datos faltantes, validar el comprobante o derivar el tramite."
        review_label = "Validar datos del documento"
        review_description = "Hay campos documentales para revisar antes de responder al solicitante."

    actions = [
        {
            "id": "operator_review",
            "label": "Enviar al equipo",
            "type": "crm",
            "description": operator_description,
            "enabled": True,
        },
        {
            "id": "open_cart",
            "label": "Ver pedido armado",
            "type": "link",
            "href": f"{tenant_path}/cart" if tenant_path else "/cart",
            "description": "Continua con los productos que quedaron asociados al catalogo.",
            "enabled": matched_count > 0 and catalog_matching,
        },
        {
            "id": "complete_by_chat",
            "label": "Completar por WhatsApp o chat",
            "type": "handoff",
            "description": complete_description,
            "enabled": True,
        },
    ]
    if unmatched_count:
        actions.insert(
            1,
            {
                "id": "review_unmatched_items",
                "label": review_label,
                "type": "crm_task",
                "description": review_description,
                "enabled": True,
            },
        )
    actions.append(
        {
            "id": "tracking",
            "label": "Seguir solicitud",
            "type": "reference",
            "reference": f"pedido:{pedido_id}",
            "description": "Referencia para estado, historial y seguimiento por canal.",
            "enabled": True,
        }
    )
    return actions


def _normalize_items(owner_id: int, tenant_id: int, rows: List[dict], *, catalog_matching: bool = True):
    pyme_carts_data: Dict[int, list] = session.get("carritos_pymes", {})
    cart = _get_pyme_cart(pyme_carts_data, tenant_id)
    not_found: List[dict] = []
    row_errors: List[dict] = []
    matched_entries: List[dict] = []

    if not catalog_matching:
        for row in rows:
            normalized_row, row_warning = _normalize_extracted_row(row)
            if row_warning:
                row_errors.append(row_warning)
            if normalized_row:
                not_found.append(normalized_row)
        return cart, not_found, row_errors, matched_entries

    query = CatalogoItem.query.filter(
        CatalogoItem.user_id == owner_id,
        func.coalesce(CatalogoItem.tenant_id, tenant_id) == tenant_id,
    )
    catalog_items = query.limit(500).all()
    for row in rows:
        normalized_row, row_warning = _normalize_extracted_row(row)
        if row_warning:
            row_errors.append(row_warning)
        if not normalized_row:
            continue
        cantidad = normalized_row["cantidad"]
        sku = normalized_row.get("sku")
        nombre = normalized_row.get("nombre")
        match = None
        if sku:
            match = query.filter(func.lower(CatalogoItem.sku) == sku.lower()).first()
        if not match and nombre:
            match = query.filter(func.lower(CatalogoItem.nombre) == nombre.lower()).first()
        if match:
            matched_entry = {"catalogo_item_id": match.id, "cantidad": cantidad}
            cart.append(matched_entry)
            matched_entries.append(matched_entry)
        else:
            candidates = _catalog_candidates_for_row(normalized_row, catalog_items)
            if candidates:
                normalized_row = {**normalized_row, "catalog_candidates": candidates}
            not_found.append(normalized_row)
    session["carritos_pymes"] = pyme_carts_data
    session.modified = True
    return cart, not_found, row_errors, matched_entries


def _extract_rows(contenido: bytes, prompt: Optional[str] = None) -> List[dict]:
    try:
        rows = extract_table_from_file(contenido, prompt or _PROMPT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error al extraer items de nota de pedido", exc_info=exc)
        raise ValueError("No se pudo procesar el archivo subido") from exc

    if rows:
        return rows

    try:
        df = pd.read_excel(io.BytesIO(contenido))
    except Exception as excel_exc:  # noqa: BLE001
        try:
            df = pd.read_csv(io.BytesIO(contenido))
        except Exception as csv_exc:  # noqa: BLE001
            logger.exception("Formato de nota de pedido no soportado", exc_info=csv_exc)
            raise ValueError("Formato de archivo no soportado o dañado") from csv_exc
    return df.to_dict(orient="records")


def _row_from_text_line(line: str) -> dict:
    normalized = line.strip(" -\t")
    match = re.match(r"^(?P<cantidad>\d+(?:[\.,]\d+)?)\s+(?P<nombre>.+)$", normalized)
    if match:
        return {"nombre": match.group("nombre").strip(), "cantidad": match.group("cantidad")}
    return {"nombre": normalized, "cantidad": 1}


def _extract_rows_from_text(text: str, prompt: Optional[str] = None) -> List[dict]:
    text_prompt = (
        f"{prompt or _PROMPT}\n\n"
        "El contenido fue pegado por el usuario como texto. Extrae renglones del pedido y respeta cantidades."
    )
    try:
        rows = extract_table_from_file(text.encode("utf-8"), text_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.info("No se pudo extraer texto pegado automaticamente; se usara fallback por renglones. error=%s", exc)
        rows = None
    if rows:
        return rows

    lines = [line.strip() for line in re.split(r"[\r\n;]+", text) if line.strip()]
    return [_row_from_text_line(line) for line in lines[:50]]


@pedidos_from_file_bp.route("/from-file", methods=["POST", "OPTIONS"])
@cross_origin(
    origins=ALLOWED_ORIGINS,
    supports_credentials=True,
    allow_headers=_CORS_ALLOWED_HEADERS,
    methods=["POST", "OPTIONS"],
)
def pedidos_desde_archivo():
    if request.method == "OPTIONS":
        return "", 204

    json_payload = request.get_json(silent=True) if request.is_json else None
    archivo = request.files.get("archivo") or request.files.get("file")
    text_payload = _clean_optional_text(
        _form_or_json_value(json_payload, "pedido_text", "notes_text", "order_text", "texto", "text")
    )
    if not archivo and not text_payload:
        return _json_error(400, "archivo_o_texto_requerido", "Archivo o texto de pedido requerido")
    if archivo and not archivo.filename:
        return _json_error(400, "archivo_sin_nombre", "Archivo sin nombre")

    extension = archivo.filename.rsplit(".", 1)[-1].lower() if archivo and "." in archivo.filename else "txt"
    allowed = {"pdf", "xls", "xlsx", "csv", "png", "jpg", "jpeg", "webp", "doc", "docx", "txt"}
    if archivo and extension not in allowed:
        return _json_error(
            415,
            "formato_no_permitido",
            "Formato no permitido. Usa PDF, Excel, documento o imagen.",
        )

    requested_kind = (
        _form_or_json_value(json_payload, "document_type", "request_kind")
        or request.args.get("document_type")
        or request.args.get("request_kind")
    )
    request_kind, request_kind_classification = _infer_request_kind(
        requested_kind,
        text_payload=text_payload,
        filename=archivo.filename if archivo else None,
    )
    request_kind, request_kind_config = _request_kind_config(request_kind)
    document_profile = _build_document_profile(
        request_kind,
        request_kind_config,
        input_type=extension,
        has_file=bool(archivo),
        classification=request_kind_classification,
    )
    chat_session_id = _clean_optional_text(
        request.headers.get("X-Chat-Session-Id")
        or _form_or_json_value(json_payload, "chat_session_id", "chatSessionId", "session_id")
        or request.args.get("chat_session_id")
    )
    request_anon_id = _clean_optional_text(
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or _form_or_json_value(json_payload, "anon_id", "anonId")
        or request.args.get("anon_id")
    )
    contact_payload = {
        "name": _clean_optional_text(_form_or_json_value(json_payload, "contact_name", "nombre")),
        "phone": _clean_optional_text(_form_or_json_value(json_payload, "contact_phone", "telefono")),
        "email": _clean_optional_text(_form_or_json_value(json_payload, "contact_email", "email")),
        "notes": _clean_optional_text(_form_or_json_value(json_payload, "contact_notes", "observaciones")),
    }
    contact_payload = {key: value for key, value in contact_payload.items() if value}

    tenant_slug = request.headers.get("X-Tenant") or request.args.get("tenant") or request.args.get("tenant_slug")
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    user = getattr(g, "user", None)
    owner = None
    try:
        tenant, user, _ = resolve_tenant_and_user(
            tenant_slug=tenant_slug,
            tenant_id=tenant_id,
            current_user=user,
            widget_token=request.headers.get("X-Widget-Token") or request.args.get("widget_token"),
        )
    except TenantResolutionError:
        tenant, owner = _resolve_public_owner()
        if not tenant or not owner:
            return _json_error(404, "tenant_no_encontrado", "Tenant no encontrado")

    if not owner:
        owner = getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)

    if archivo:
        try:
            contenido = archivo.read()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error al leer archivo de nota de pedido", exc_info=exc)
            return _json_error(400, "archivo_ilegible", "No se pudo leer el archivo subido")
    else:
        if len(text_payload or "") > _MAX_ORDER_TEXT_CHARS:
            return _json_error(
                413,
                "texto_demasiado_largo",
                "El texto supera el limite permitido. Usa hasta 12000 caracteres.",
            )
        contenido = (text_payload or "").encode("utf-8")

    if not contenido:
        return _json_error(400, "archivo_vacio", "El archivo está vacío")
    if len(contenido) > _MAX_ORDER_NOTE_BYTES:
        return _json_error(
            413,
            "archivo_demasiado_grande",
            "El archivo supera el limite permitido. Usa un archivo de hasta 8 MB.",
        )

    if archivo:
        _reset_file_pointer(archivo)
        upload_meta = upload_to_gcs(archivo)
        if not upload_meta or not upload_meta.get("public_url"):
            return _json_error(500, "upload_fallido", "No se pudo guardar el archivo")
        original_name = upload_meta.get("original_name") or archivo.filename
    else:
        upload_meta = {"public_url": None, "original_name": "pedido-escrito.txt"}
        original_name = "pedido escrito"

    extraction_error = None
    try:
        rows = (
            _extract_rows(contenido, request_kind_config.get("prompt"))
            if archivo
            else _extract_rows_from_text(text_payload or "", request_kind_config.get("prompt"))
        )
    except ValueError as exc:
        rows = []
        extraction_error = str(exc)
        logger.info(
            "Archivo recibido sin extraccion estructurada; se creara solicitud para revision manual. kind=%s error=%s",
            request_kind,
            extraction_error,
        )
    except Exception as exc:  # noqa: BLE001
        rows = []
        extraction_error = "Error interno al interpretar el archivo"
        logger.exception("Error inesperado procesando nota de pedido", exc_info=exc)

    request_kind, request_kind_classification, request_kind_config = _maybe_reclassify_request_kind_from_extraction(
        current_kind=request_kind,
        current_classification=request_kind_classification,
        rows=rows or [],
        text_payload=text_payload,
        filename=archivo.filename if archivo else None,
    )
    document_profile = _build_document_profile(
        request_kind,
        request_kind_config,
        input_type=extension,
        has_file=bool(archivo),
        classification=request_kind_classification,
    )

    catalog_matching_enabled = bool(document_profile.get("catalog_matching"))
    cart, not_found, row_errors, matched_entries = _normalize_items(
        owner.id,
        tenant.id,
        rows or [],
        catalog_matching=catalog_matching_enabled,
    )

    # Enriquecer respuesta reutilizando formateador existente
    item_ids = [it.get("catalogo_item_id") for it in matched_entries if it.get("catalogo_item_id")]
    enriched = []
    if item_ids:
        items = CatalogoItem.query.filter(CatalogoItem.id.in_(item_ids)).all()
        mapping = {item.id: item for item in items}
        for it in matched_entries:
            item = mapping.get(it.get("catalogo_item_id"))
            if not item:
                continue
            formatted = _formatear_producto(
                {
                    "nombre": item.nombre,
                    "descripcion": item.descripcion,
                    "precio_str": item.precio,
                    "precio_float": float(item.precio_monetario) if item.precio_monetario is not None else None,
                    "sku": item.sku,
                    "unidad": item.unidad,
                    "imagen_url": item.imagen_url,
                }
            )
            enriched.append({"catalogo_item_id": item.id, "cantidad": it.get("cantidad", 1), **formatted})

    origen = request.headers.get("X-Checkout-Origin") or request.args.get("origen") or "web"
    tenant_slug_resolved = getattr(tenant, "slug", None) or tenant_slug
    unmatched_labels = [_row_label(row) for row in not_found]
    catalog_candidates = _unmatched_catalog_candidates_payload(not_found) if catalog_matching_enabled else []
    matched_count = len(enriched)
    unmatched_count = len(not_found)
    detected_count = matched_count + unmatched_count
    request_kind_label = request_kind_config.get("label") or "archivo"
    primary_intent = str(document_profile.get("primary_intent") or "")
    is_service_request = primary_intent == "municipal_service_request"
    is_document_review = not catalog_matching_enabled and not is_service_request
    if extraction_error:
        customer_message = (
            f"Recibimos tu {request_kind_label}. No pudimos extraer renglones con suficiente seguridad, "
            "pero ya quedo cargada para revision del equipo."
        )
    elif is_service_request:
        customer_message = (
            f"Recibimos tu {request_kind_label}. La solicitud quedo cargada para validar direccion, categoria, "
            "urgencia y area responsable."
        )
        if unmatched_count:
            customer_message = (
                f"Recibimos tu {request_kind_label} con {unmatched_count} punto(s) detectado(s). "
                "El equipo lo revisa para derivarlo al area correspondiente y responderte por este canal."
            )
    elif is_document_review:
        customer_message = (
            f"Recibimos tu {request_kind_label}. Ya separamos los datos utiles y el equipo puede validar "
            "el documento o tramite."
        )
        if unmatched_count:
            customer_message = (
                f"Recibimos tu {request_kind_label} con {unmatched_count} dato(s) para revision. "
                "El equipo valida la informacion y te responde el proximo paso."
            )
    elif matched_count and unmatched_count:
        customer_message = (
            f"Recibimos tu {request_kind_label}: encontramos {matched_count} articulo(s) del catalogo y "
            f"{unmatched_count} renglon(es) quedan para revision del equipo."
        )
    elif matched_count:
        customer_message = f"Recibimos tu {request_kind_label} y armamos un borrador con {matched_count} articulo(s) del catalogo."
    elif unmatched_count:
        customer_message = (
            f"Recibimos tu {request_kind_label}. No pudimos asociar articulos al catalogo automaticamente, "
            "pero el equipo la tiene lista para revisar y responder."
        )
    else:
        customer_message = (
            f"Recibimos tu {request_kind_label}. Ya separamos los datos detectados y el equipo puede revisar "
            "faltantes, stock, precios y datos de entrega."
        )
    source_payload = {
        "channel": origen,
        "input_type": extension,
        "request_kind": request_kind,
        "request_kind_label": request_kind_label,
        "classification": request_kind_classification,
        "archivo_url": upload_meta.get("public_url"),
        "archivo_nombre": original_name,
        "original_filename": original_name,
        "mime_type": (
            _clean_optional_text(getattr(archivo, "mimetype", None) if archivo else None)
            or mimetypes.guess_type(original_name or "")[0]
            or ("text/plain" if text_payload else "application/octet-stream")
        ),
        "file_size_bytes": len(contenido or b""),
    }
    if chat_session_id:
        source_payload["chat_session_id"] = chat_session_id
    if request_anon_id:
        source_payload["anon_id"] = request_anon_id
    if text_payload:
        source_payload["text_preview"] = text_payload[:500]
    if extraction_error:
        source_payload["extraction_error"] = extraction_error
    if row_errors:
        source_payload["row_errors"] = row_errors
    structured_extraction = _build_structured_extraction(
        document_profile=document_profile,
        rows=rows or [],
        text_payload=text_payload,
        contact_payload=contact_payload,
    )
    structured_missing_fields = (
        structured_extraction.get("missing_fields")
        if isinstance(structured_extraction.get("missing_fields"), list)
        else []
    )
    crm_handoff = _build_crm_handoff_payload(
        document_profile=document_profile,
        structured_extraction=structured_extraction,
        source_payload=source_payload,
        contact_payload=contact_payload,
    )
    match_summary = {
        "matched": matched_count,
        "unmatched": unmatched_count,
        "detected": detected_count,
        "needs_operator_review": (
            unmatched_count > 0
            or matched_count == 0
            or bool(extraction_error)
            or bool(structured_missing_fields)
            or not bool(contact_payload.get("phone") or contact_payload.get("email"))
        ),
    }
    crm_state = "pending_operator_review" if match_summary["needs_operator_review"] else "ready_for_confirmation"
    crm_order_draft = None
    if catalog_matching_enabled:
        crm_order_draft = build_crm_order_draft(
            request_kind=request_kind,
            request_kind_label=request_kind_label,
            source=source_payload,
            contact=contact_payload,
            matched_items=enriched,
            unmatched_items=not_found,
            catalog_candidates=catalog_candidates,
            match_summary=match_summary,
        )
        crm_handoff["draft_order"] = crm_order_draft
    review_context = _build_review_context(
        document_profile=document_profile,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        catalog_candidates=catalog_candidates,
        extraction_error=extraction_error,
        contact_payload=contact_payload,
        missing_fields=structured_missing_fields,
    )
    customer_next_steps = _build_customer_next_steps(
        document_profile=document_profile,
        request_kind_label=request_kind_label,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        extraction_error=extraction_error,
    )
    intake_experience = _build_intake_experience(
        document_profile=document_profile,
        request_kind_label=request_kind_label,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        detected_count=detected_count,
        extraction_error=extraction_error,
        contact_payload=contact_payload,
    )
    operator_pack = _build_assisted_operator_pack(
        request_kind_label=request_kind_label,
        contact=contact_payload,
        match_summary=match_summary,
        unmatched_items=unmatched_labels,
        extraction_error=extraction_error,
        primary_intent=document_profile.get("primary_intent"),
        target_module=crm_handoff.get("target_module"),
        missing_fields=structured_missing_fields,
    )
    next_actions = _build_next_actions(
        pedido_id=0,
        tenant_slug=tenant_slug_resolved,
        document_profile=document_profile,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
    )

    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=getattr(user, "id", None) or owner.id,
        tipo=request_kind_config.get("crm_type") or "nota_de_pedido",
        estado="nuevo",
        items=[
            {
                "archivo_url": upload_meta.get("public_url"),
                "archivo_nombre": original_name,
                "texto_original": text_payload,
                "request_kind": request_kind,
                "request_kind_label": request_kind_label,
                "contact": contact_payload,
                "items_detectados": enriched,
                "no_encontrados": not_found,
                "no_encontrados_labels": unmatched_labels,
                "catalog_candidates": catalog_candidates,
                "origen": origen,
                "contract_version": _ASSISTED_REQUEST_CONTRACT_VERSION,
                "document_profile": document_profile,
                "structured_extraction": structured_extraction,
                "crm_handoff": crm_handoff,
                "crm_order_draft": crm_order_draft,
                "review_context": review_context,
                "match_summary": match_summary,
                "customer_message": customer_message,
                "customer_next_steps": customer_next_steps,
                "intake_experience": intake_experience,
                "operator_pack": operator_pack,
                "extraction_error": extraction_error,
                "row_errors": row_errors,
            }
        ],
        monto_monetario=0,
        monto_puntos=0,
        anon_id=request_anon_id or getattr(user, "anon_id", None),
        origen=origen,
        metadata_payload={
            "contract_version": _ASSISTED_REQUEST_CONTRACT_VERSION,
            "mode": "order_note_upload",
            "request_kind": request_kind,
            "request_kind_label": request_kind_label,
            "document_profile": document_profile,
            "structured_extraction": structured_extraction,
            "crm_handoff": crm_handoff,
            "crm_order_draft": crm_order_draft,
            "contact": contact_payload,
            "crm_state": crm_state,
            "source": source_payload,
            "match_summary": match_summary,
            "review_context": review_context,
            "catalog_candidates": catalog_candidates,
            "row_errors": row_errors,
            "customer_next_steps": customer_next_steps,
            "intake_experience": intake_experience,
            "operator_pack": operator_pack,
            "next_actions": next_actions,
        },
    )
    db.session.add(pedido)
    db.session.flush()
    linked_record = _materialize_municipal_claim_from_handoff(
        pedido=pedido,
        tenant=tenant,
        owner=owner,
        user=user,
        document_profile=document_profile,
        structured_extraction=structured_extraction,
        crm_handoff=crm_handoff,
        source_payload=source_payload,
        contact_payload=contact_payload,
        rows=rows or [],
        text_payload=text_payload,
        upload_meta=upload_meta,
        origen=origen,
    )
    public_follow_up = _build_public_follow_up(
        pedido_id=pedido.id,
        tenant_slug=tenant_slug_resolved,
        request_kind_label=request_kind_label,
        customer_message=customer_message,
        linked_claim=linked_record,
        whatsapp_phone=getattr(tenant, "dispatch_phone", None)
        if getattr(tenant, "send_dispatch_whatsapp", True)
        else None,
    )
    if crm_order_draft:
        crm_order_draft = {
            **crm_order_draft,
            "pedido_id": pedido.id,
            "lead_id": pedido.id,
            "reference": f"pedido:{pedido.id}",
        }
        crm_handoff["draft_order"] = crm_order_draft
    operator_intake_summary = _build_operator_intake_summary(
        document_profile=document_profile,
        request_kind_label=request_kind_label,
        source_payload=source_payload,
        contact_payload=contact_payload,
        match_summary=match_summary,
        crm_handoff=crm_handoff,
        enriched_items=enriched,
        unmatched_labels=unmatched_labels,
        public_follow_up=public_follow_up,
    )
    for action in next_actions:
        if action.get("id") == "tracking":
            action["reference"] = (
                f"municipio_ticket:{linked_record['id']}" if linked_record else f"pedido:{pedido.id}"
            )
            action["type"] = "link"
            action["href"] = public_follow_up["tracking"]["path"]
            action["tracking_code"] = public_follow_up["tracking"]["code"]
            action["tracking_kind"] = public_follow_up["tracking"]["kind"]
    for channel in public_follow_up.get("channels", []):
        if not isinstance(channel, dict) or channel.get("id") == "tracking_page":
            continue
        if not any(action.get("id") == channel.get("id") for action in next_actions):
            next_actions.append({**channel, "enabled": True})
    operator_pack = _build_assisted_operator_pack(
        record_id=pedido.id,
        request_kind_label=request_kind_label,
        contact=contact_payload,
        match_summary=match_summary,
        unmatched_items=unmatched_labels,
        extraction_error=extraction_error,
        primary_intent=document_profile.get("primary_intent"),
        target_module=crm_handoff.get("target_module"),
        missing_fields=structured_missing_fields,
    )
    metadata_payload = dict(pedido.metadata_payload or {})
    metadata_payload["next_actions"] = next_actions
    metadata_payload["operator_pack"] = operator_pack
    metadata_payload["public_follow_up"] = public_follow_up
    metadata_payload["crm_handoff"] = crm_handoff
    metadata_payload["crm_order_draft"] = crm_order_draft
    metadata_payload["operator_intake_summary"] = operator_intake_summary
    if linked_record:
        metadata_payload["linked_record"] = linked_record
        metadata_payload["crm_state"] = "materialized_ticket_pending_review"
    pedido.metadata_payload = metadata_payload
    if pedido.items and isinstance(pedido.items[0], dict):
        first_item_payload = dict(pedido.items[0])
        first_item_payload["operator_pack"] = operator_pack
        first_item_payload["public_follow_up"] = public_follow_up
        first_item_payload["crm_handoff"] = crm_handoff
        first_item_payload["crm_order_draft"] = crm_order_draft
        first_item_payload["operator_intake_summary"] = operator_intake_summary
        if linked_record:
            first_item_payload["linked_record"] = linked_record
        pedido.items = [first_item_payload, *pedido.items[1:]]
    db.session.commit()

    response_payload = {
        "contract_version": _ASSISTED_REQUEST_CONTRACT_VERSION,
        "mode": "order_note_upload",
        "request_kind": request_kind,
        "request_kind_label": request_kind_label,
        "document_profile": document_profile,
        "structured_extraction": structured_extraction,
        "crm_handoff": crm_handoff,
        "crm_order_draft": crm_order_draft,
        "tenant_id": tenant.id,
        "tenant_slug": tenant_slug_resolved,
        "items": enriched,
        "no_encontrados": not_found,
        "items_no_encontrados": unmatched_labels,
        "catalog_candidates": catalog_candidates,
        "pedido_id": pedido.id,
        "lead_id": pedido.id,
        "archivo_url": upload_meta.get("public_url"),
        "tipo": pedido.tipo,
        "contact": contact_payload,
        "source": source_payload,
        "crm_state": pedido.metadata_payload.get("crm_state"),
        "match_summary": match_summary,
        "review_context": review_context,
        "customer_next_steps": customer_next_steps,
        "intake_experience": intake_experience,
        "operator_pack": operator_pack,
        "operator_intake_summary": operator_intake_summary,
        "public_follow_up": public_follow_up,
        "row_errors": row_errors,
        "next_actions": next_actions,
        "customer_message": customer_message,
        "resumen": customer_message,
    }
    if linked_record:
        response_payload.update(
            {
                "linked_record": linked_record,
                "ticket_id": linked_record.get("id"),
                "nro_ticket": linked_record.get("display_code") or linked_record.get("nro_ticket"),
                "consulta_pin": linked_record.get("consulta_pin"),
                "ticket_type": "municipio",
            }
        )

    return (
        jsonify(response_payload),
        201,
    )
