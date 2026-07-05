from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from typing import Any, Optional

from database import db
from models import CatalogoItem, PedidoConversacional, TenantProfile, TenantTicket, User
from routes.catalogo import _formatear_producto
from routes.pedidos_from_file import (
    _ASSISTED_REQUEST_CONTRACT_VERSION,
    _build_crm_handoff_payload,
    _build_document_profile,
    _build_review_context,
    _build_structured_extraction,
    _extract_rows,
    _extract_rows_from_text,
    _infer_request_kind,
    _maybe_reclassify_request_kind_from_extraction,
    _normalize_items,
    _request_kind_config,
    _row_label,
    _unmatched_catalog_candidates_payload,
)
from services.commerce_unified import _build_assisted_operator_pack
from services.marketplace_analytics import track_marketplace_event

logger = logging.getLogger(__name__)

WHATSAPP_ASSISTED_INTAKE_CONTRACT_VERSION = "whatsapp.assisted_intake.v1"
_MAX_WHATSAPP_INTAKE_BYTES = 8 * 1024 * 1024
_MAX_WHATSAPP_TEXT_CHARS = 12000
_TRUE_VALUES = {"1", "true", "yes", "si", "on", "enabled"}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_VALUES


def _config_lookup(config: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if key in config:
            return config.get(key)
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    whatsapp = config.get("whatsapp") if isinstance(config.get("whatsapp"), dict) else {}
    intake = config.get("assisted_intake") if isinstance(config.get("assisted_intake"), dict) else {}
    for nested in (features, whatsapp, intake):
        for key in keys:
            if key in nested:
                return nested.get(key)
    return None


def whatsapp_assisted_intake_enabled(tenant: Optional[TenantProfile], owner_user: Optional[User] = None) -> bool:
    """Feature flag for turning WhatsApp note/document intake into CRM work."""

    if _truthy(os.getenv("WHATSAPP_ASSISTED_INTAKE_V1")):
        return True
    config = getattr(tenant, "configuracion", None) if tenant else None
    if isinstance(config, dict) and _truthy(
        _config_lookup(
            config,
            "whatsapp_assisted_intake_v1",
            "whatsapp_assisted_intake",
            "assisted_intake_whatsapp",
            "order_note_intake",
        )
    ):
        return True
    owner_config = getattr(owner_user, "configuracion", None) if owner_user else None
    if isinstance(owner_config, dict):
        return _truthy(
            _config_lookup(
                owner_config,
                "whatsapp_assisted_intake_v1",
                "whatsapp_assisted_intake",
                "assisted_intake_whatsapp",
                "order_note_intake",
            )
        )
    return False


def _extension_from_media(filename: Optional[str], mime_type: Optional[str]) -> str:
    candidate = (filename or "").rsplit(".", 1)
    if len(candidate) == 2 and candidate[-1].strip():
        return candidate[-1].lower().strip()
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    return guessed.lstrip(".").lower() or "txt"


def _contact_payload(end_user: Optional[User], from_number: Optional[str]) -> dict[str, str]:
    payload = {
        "name": getattr(end_user, "name", None),
        "phone": from_number or getattr(end_user, "telefono", None),
        "email": getattr(end_user, "email", None),
    }
    return {key: str(value) for key, value in payload.items() if value}


def _build_description(
    *,
    request_kind_label: str,
    text_payload: Optional[str],
    rows: list[dict],
    matched_count: int,
    unmatched_count: int,
) -> str:
    parts = [f"Ingesta asistida por WhatsApp: {request_kind_label}."]
    if text_payload:
        parts.append(text_payload[:700])
    if rows:
        preview = "; ".join(_row_label(row) for row in rows[:8] if _row_label(row))
        if preview:
            parts.append(f"Detectado: {preview}")
    parts.append(f"Resumen IA: {matched_count} item(s) de catalogo, {unmatched_count} item(s) para revisar.")
    return "\n".join(parts).strip()


def _matched_items_payload(matched_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    item_ids = [item.get("catalogo_item_id") for item in matched_entries if item.get("catalogo_item_id")]
    if not item_ids:
        return []
    catalog_items = CatalogoItem.query.filter(CatalogoItem.id.in_(item_ids)).all()
    mapping = {item.id: item for item in catalog_items}
    enriched: list[dict[str, Any]] = []
    for entry in matched_entries:
        item = mapping.get(entry.get("catalogo_item_id"))
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
        enriched.append({"catalogo_item_id": item.id, "cantidad": entry.get("cantidad", 1), **formatted})
    return enriched


def _fingerprint(tenant_id: int, from_number: Optional[str], content: bytes, text_payload: Optional[str]) -> str:
    digest = hashlib.sha1()
    digest.update(str(tenant_id).encode("utf-8"))
    digest.update((from_number or "").encode("utf-8"))
    digest.update(content[:2048])
    digest.update((text_payload or "").encode("utf-8")[:2048])
    return f"wa-intake:{digest.hexdigest()[:32]}"


def create_whatsapp_assisted_intake(
    *,
    tenant: TenantProfile,
    owner_user: User,
    end_user: Optional[User],
    session_id: str,
    from_number: str,
    message_body: Optional[str],
    uploaded_file_info: Optional[dict[str, Any]],
    media_bytes: Optional[bytes],
    location_info: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if not whatsapp_assisted_intake_enabled(tenant, owner_user):
        return None

    mime_type = str((uploaded_file_info or {}).get("mime_type") or "").lower()
    if mime_type.startswith("audio/"):
        return None

    text_payload = (message_body or "").strip() or None
    content = media_bytes or (text_payload.encode("utf-8") if text_payload else b"")
    if not content:
        return None
    if len(content) > _MAX_WHATSAPP_INTAKE_BYTES:
        logger.info("WhatsApp assisted intake skipped: file too large tenant=%s", getattr(tenant, "slug", None))
        return {
            "created": False,
            "error": "archivo_demasiado_grande",
            "customer_message": "Recibimos el archivo, pero supera el limite para analizarlo automaticamente.",
        }
    if text_payload and len(text_payload) > _MAX_WHATSAPP_TEXT_CHARS:
        text_payload = text_payload[:_MAX_WHATSAPP_TEXT_CHARS]

    filename = (uploaded_file_info or {}).get("name") or "whatsapp-message.txt"
    extension = _extension_from_media(filename, mime_type)
    request_kind, request_kind_classification = _infer_request_kind(
        None,
        text_payload=text_payload,
        filename=filename,
    )
    request_kind, request_kind_config = _request_kind_config(request_kind)
    has_file = bool(media_bytes)

    extraction_error = None
    try:
        rows = (
            _extract_rows(content, request_kind_config.get("prompt"))
            if has_file
            else _extract_rows_from_text(text_payload or "", request_kind_config.get("prompt"))
        )
    except ValueError as exc:
        rows = []
        extraction_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        rows = []
        extraction_error = "Error interno al interpretar el adjunto de WhatsApp"
        logger.exception("Unexpected WhatsApp assisted intake extraction error", exc_info=exc)

    request_kind, request_kind_classification, request_kind_config = _maybe_reclassify_request_kind_from_extraction(
        current_kind=request_kind,
        current_classification=request_kind_classification,
        rows=rows or [],
        text_payload=text_payload,
        filename=filename,
    )
    document_profile = _build_document_profile(
        request_kind,
        request_kind_config,
        input_type=extension,
        has_file=has_file,
        classification=request_kind_classification,
    )
    catalog_matching_enabled = bool(document_profile.get("catalog_matching"))
    _, not_found, row_errors, matched_entries = _normalize_items(
        owner_user.id,
        tenant.id,
        rows or [],
        catalog_matching=catalog_matching_enabled,
        mutate_cart=False,
    )
    enriched = _matched_items_payload(matched_entries)
    matched_count = len(enriched)
    unmatched_count = len(not_found)
    detected_count = matched_count + unmatched_count
    request_kind_label = request_kind_config.get("label") or "archivo"
    contact = _contact_payload(end_user, from_number)
    source_payload: dict[str, Any] = {
        "channel": "whatsapp",
        "surface": "whatsapp_assisted_intake",
        "input_type": extension,
        "request_kind": request_kind,
        "request_kind_label": request_kind_label,
        "classification": request_kind_classification,
        "archivo_url": (uploaded_file_info or {}).get("url"),
        "archivo_nombre": filename,
        "original_filename": filename,
        "mime_type": mime_type or mimetypes.guess_type(filename)[0] or ("text/plain" if text_payload else "application/octet-stream"),
        "file_size_bytes": len(content),
        "attachment_id": (uploaded_file_info or {}).get("id"),
        "chat_session_id": session_id,
        "anon_id": from_number,
    }
    if text_payload:
        source_payload["text_preview"] = text_payload[:500]
    if location_info:
        source_payload["location"] = location_info
    if idempotency_key:
        source_payload["idempotency_key"] = idempotency_key
    if extraction_error:
        source_payload["extraction_error"] = extraction_error
    if row_errors:
        source_payload["row_errors"] = row_errors

    structured_extraction = _build_structured_extraction(
        document_profile=document_profile,
        rows=rows or [],
        text_payload=text_payload,
        contact_payload=contact,
    )
    missing_fields = structured_extraction.get("missing_fields") if isinstance(structured_extraction.get("missing_fields"), list) else []
    crm_handoff = _build_crm_handoff_payload(
        document_profile=document_profile,
        structured_extraction=structured_extraction,
        source_payload=source_payload,
        contact_payload=contact,
    )
    catalog_candidates = _unmatched_catalog_candidates_payload(not_found) if catalog_matching_enabled else []
    match_summary = {
        "matched": matched_count,
        "unmatched": unmatched_count,
        "detected": detected_count,
        "needs_operator_review": bool(unmatched_count or matched_count == 0 or extraction_error or missing_fields),
    }
    review_context = _build_review_context(
        document_profile=document_profile,
        matched_count=matched_count,
        unmatched_count=unmatched_count,
        catalog_candidates=catalog_candidates,
        extraction_error=extraction_error,
        contact_payload=contact,
        missing_fields=missing_fields,
    )
    unmatched_labels = [_row_label(row) for row in not_found]
    operator_pack = _build_assisted_operator_pack(
        request_kind_label=request_kind_label,
        contact=contact,
        match_summary=match_summary,
        unmatched_items=unmatched_labels,
        extraction_error=extraction_error,
        primary_intent=document_profile.get("primary_intent"),
        target_module=crm_handoff.get("target_module"),
        missing_fields=missing_fields,
    )
    customer_message = (
        f"Recibimos tu {request_kind_label} por WhatsApp. "
        "La IA separo la informacion y el equipo ya tiene un borrador operativo en el CRM."
    )
    if unmatched_count:
        customer_message += f" Quedan {unmatched_count} renglon(es) para validar manualmente."

    fingerprint = _fingerprint(tenant.id, from_number, content, text_payload)
    existing_ticket = TenantTicket.query.filter_by(tenant_id=tenant.id, fingerprint=fingerprint).first()
    if existing_ticket:
        return {
            "created": False,
            "idempotent_replay": True,
            "ticket_id": existing_ticket.id,
            "customer_message": "Ya teniamos ese archivo cargado para revision. Lo mantenemos asociado al mismo caso.",
        }

    payload = {
        "contract_version": WHATSAPP_ASSISTED_INTAKE_CONTRACT_VERSION,
        "assisted_request_contract_version": _ASSISTED_REQUEST_CONTRACT_VERSION,
        "mode": "whatsapp_order_note_upload",
        "request_kind": request_kind,
        "request_kind_label": request_kind_label,
        "document_profile": document_profile,
        "structured_extraction": structured_extraction,
        "crm_handoff": crm_handoff,
        "source": source_payload,
        "contact": contact,
        "items": enriched,
        "no_encontrados": not_found,
        "items_no_encontrados": unmatched_labels,
        "catalog_candidates": catalog_candidates,
        "match_summary": match_summary,
        "review_context": review_context,
        "operator_pack": operator_pack,
        "customer_message": customer_message,
    }

    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=getattr(end_user, "id", None) or owner_user.id,
        tipo=request_kind_config.get("crm_type") or "nota_de_pedido",
        estado="nuevo",
        monto_monetario=0,
        monto_puntos=0,
        anon_id=from_number,
        origen="whatsapp",
        items=[payload],
        metadata_payload={**payload, "crm_state": "pending_operator_review"},
    )
    db.session.add(pedido)
    db.session.flush()

    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=getattr(end_user, "id", None),
        categoria=str(crm_handoff.get("target_module") or request_kind or "whatsapp_assisted_intake")[:80],
        descripcion=_build_description(
            request_kind_label=request_kind_label,
            text_payload=text_payload,
            rows=rows or [],
            matched_count=matched_count,
            unmatched_count=unmatched_count,
        ),
        estado="nuevo",
        origen="whatsapp",
        datos_extra={
            **payload,
            "pedido_id": pedido.id,
            "lead_id": pedido.id,
            "ticket_contract_version": "whatsapp.assisted_intake_ticket.v1",
        },
        fingerprint=fingerprint,
    )
    db.session.add(ticket)
    db.session.flush()
    payload["pedido_id"] = pedido.id
    payload["lead_id"] = pedido.id
    payload["ticket_id"] = ticket.id
    pedido.metadata_payload = {**(pedido.metadata_payload or {}), "ticket_id": ticket.id}
    db.session.commit()

    try:
        track_marketplace_event(
            tenant,
            "whatsapp_assisted_intake_created",
            {
                "source": "whatsapp_assisted_intake",
                "contract_version": WHATSAPP_ASSISTED_INTAKE_CONTRACT_VERSION,
                "request_kind": request_kind,
                "request_kind_label": request_kind_label,
                "primary_intent": document_profile.get("primary_intent"),
                "target_module": crm_handoff.get("target_module"),
                "input_type": extension,
                "has_file": has_file,
                "matched_count": matched_count,
                "unmatched_count": unmatched_count,
                "detected_count": detected_count,
                "needs_operator_review": match_summary.get("needs_operator_review"),
                "ticket_id": ticket.id,
                "pedido_id": pedido.id,
                "extraction_error": bool(extraction_error),
            },
            channel="whatsapp",
            session_id=session_id,
            anon_id=from_number,
            entity_ref=f"tenant_ticket:{ticket.id}",
            user_id=getattr(end_user, "id", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("WhatsApp assisted intake analytics event failed: %s", exc)

    return {
        "created": True,
        "contract_version": WHATSAPP_ASSISTED_INTAKE_CONTRACT_VERSION,
        "pedido_id": pedido.id,
        "lead_id": pedido.id,
        "ticket_id": ticket.id,
        "request_kind": request_kind,
        "request_kind_label": request_kind_label,
        "match_summary": match_summary,
        "customer_message": customer_message,
        "payload": payload,
    }
