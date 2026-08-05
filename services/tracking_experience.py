from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from flask import current_app, has_app_context

from models import ArchivoAdjunto, MarketOrder, MunicipioTicket, OrderEvent, PedidoConversacional, PymePedido, TenantProfile, TenantTicket, TicketComentario
from services.attachment_delivery import serialize_attachment_for_delivery
from services.live_chat_access import attach_ticket_room_access, build_ticket_room
from services.live_chat_schedule import build_tenant_live_chat_status


TRACKING_EXPERIENCE_CONTRACT_VERSION = "tracking.experience.v1"
ORDER_TRACKING_TOKEN_VERSION = "v1"


def _tracking_path_without_query_secret(path: Any) -> str:
    """Move legacy tracking credentials from query strings into URL fragments."""

    raw = str(path or "").strip()
    if not raw:
        return raw
    parts = urlsplit(raw)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    safe_query: list[tuple[str, str]] = []
    secret_items: list[tuple[str, str]] = []
    for key, value in query_items:
        if key.lower() in {"token", "access_token", "pin"}:
            canonical = "token" if key.lower() in {"token", "access_token"} else "pin"
            secret_items.append((canonical, value))
        else:
            safe_query.append((key, value))
    if not secret_items:
        return raw

    fragment_items = parse_qsl(parts.fragment, keep_blank_values=True)
    existing_keys = {key for key, _value in fragment_items}
    fragment_items.extend((key, value) for key, value in secret_items if key not in existing_keys)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(safe_query),
            urlencode(fragment_items),
        )
    )

CLAIM_MILESTONES = [
    {"key": "recibido", "label": "Recibido"},
    {"key": "validando", "label": "Validando"},
    {"key": "asignado", "label": "Asignado"},
    {"key": "en_proceso", "label": "En proceso"},
    {"key": "resuelto", "label": "Resuelto"},
    {"key": "cerrado", "label": "Cerrado"},
]

ORDER_MILESTONES = [
    {"key": "recibido", "label": "Recibido"},
    {"key": "confirmado", "label": "Confirmado"},
    {"key": "pendiente_pago", "label": "Pago"},
    {"key": "pagado", "label": "Pagado"},
    {"key": "preparando", "label": "Preparando"},
    {"key": "en_camino", "label": "En camino"},
    {"key": "entregado", "label": "Entregado"},
]

_CLAIM_STATUS_TO_STAGE = {
    "nuevo": "recibido",
    "open": "recibido",
    "abierto": "recibido",
    "pendiente": "validando",
    "validando": "validando",
    "asignado": "asignado",
    "en_proceso": "en_proceso",
    "in_progress": "en_proceso",
    "resuelto": "resuelto",
    "resolved": "resuelto",
    "cerrado": "cerrado",
    "closed": "cerrado",
}

_ORDER_STATUS_TO_STAGE = {
    "open": "recibido",
    "submitted": "recibido",
    "pending": "confirmado",
    "pendiente": "confirmado",
    "confirmed": "confirmado",
    "confirmado": "confirmado",
    "pending_payment": "pendiente_pago",
    "pendiente_pago": "pendiente_pago",
    "paid": "pagado",
    "pagado": "pagado",
    "processing": "preparando",
    "preparing": "preparando",
    "preparando": "preparando",
    "shipped": "en_camino",
    "enviado": "en_camino",
    "en_camino": "en_camino",
    "delivered": "entregado",
    "entregado": "entregado",
    "completed": "entregado",
    "completado": "entregado",
}


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tenant_ref(tenant: TenantProfile | None) -> dict[str, Any] | None:
    if tenant is None:
        return None
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "logo_url": tenant.logo_url,
        "theme_config": tenant.get_theme_config() if hasattr(tenant, "get_theme_config") else {},
    }


def _stage_payload(raw_status: Any, *, kind: str) -> dict[str, Any]:
    normalized = str(raw_status or "").strip().lower()
    if kind == "claim":
        milestones = CLAIM_MILESTONES
        current = _CLAIM_STATUS_TO_STAGE.get(normalized, "recibido")
    else:
        milestones = ORDER_MILESTONES
        current = _ORDER_STATUS_TO_STAGE.get(normalized, "recibido")

    current_index = next((idx for idx, item in enumerate(milestones) if item["key"] == current), 0)
    items = []
    for idx, item in enumerate(milestones):
        state = "pending"
        if idx < current_index:
            state = "complete"
        elif idx == current_index:
            state = "current"
        items.append({**item, "state": state, "index": idx})

    progress = round((current_index / max(1, len(milestones) - 1)) * 100, 2)
    return {
        "raw_status": raw_status,
        "current_stage": current,
        "progress_percent": progress,
        "milestones": items,
    }


def _tracking_map(location: dict[str, Any]) -> dict[str, Any]:
    has_coordinates = location.get("lat") is not None and location.get("lng") is not None
    address = location.get("address")
    maps_search_url = None
    if has_coordinates:
        maps_search_url = f"https://maps.google.com/?q={location.get('lat')},{location.get('lng')}"
    elif address:
        maps_search_url = f"https://maps.google.com/?q={quote_plus(str(address))}"

    return {
        "enabled": True,
        "has_coordinates": bool(has_coordinates),
        "center": {"lat": location.get("lat"), "lng": location.get("lng")} if has_coordinates else None,
        "maps_search_url": maps_search_url,
        "layers": ["origin", "current_status", "destination_or_claim_location", "timeline_events"],
        "animations": ["pulse_current_step", "route_progress", "status_transition"],
        "fallback_when_no_coordinates": "timeline_only",
    }


def _comment_timeline(
    comments_rel: Any,
    *,
    evidence_index: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not hasattr(comments_rel, "order_by"):
        return []
    try:
        comments = comments_rel.order_by(
            TicketComentario.fecha.asc(),
            TicketComentario.id.asc(),
        ).limit(20).all()
    except Exception:
        try:
            comments = comments_rel.order_by("fecha").limit(20).all()
        except Exception:
            comments = []

    items = []
    for comment in comments:
        attachments: list[dict[str, Any]] = []
        attachment = getattr(comment, "archivo_adjunto", None)
        if attachment is not None:
            attachments.append(
                _claim_attachment_payload(
                    attachment,
                    evidence=(evidence_index or {}).get(attachment.id),
                    fallback_source=getattr(comment, "origen", None) or "public_tracking",
                )
            )
        items.append(
            {
                "id": getattr(comment, "id", None),
                "type": "comment",
                "label": "Mensaje",
                "message": getattr(comment, "comentario", None),
                "author": "team" if getattr(comment, "es_admin", False) else "customer",
                "created_at": _iso(getattr(comment, "fecha", None)),
                "source": getattr(comment, "origen", None),
                "attachments": attachments,
            }
        )
    return items


def _claim_evidence_index(ticket: MunicipioTicket) -> dict[int, dict[str, Any]]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    batches = extra.get("whatsapp_flow_evidence")
    if not isinstance(batches, list):
        return {}
    index: dict[int, dict[str, Any]] = {}
    for batch in batches:
        if not isinstance(batch, Mapping):
            continue
        for item in batch.get("items") if isinstance(batch.get("items"), list) else []:
            if not isinstance(item, Mapping):
                continue
            try:
                attachment_id = int(item.get("attachment_id"))
            except (TypeError, ValueError):
                continue
            index[attachment_id] = {
                "source": batch.get("source") or "whatsapp_flow",
                "origin": batch.get("source") or "whatsapp_flow",
                "status": item.get("status") or "ready",
                "kind": item.get("kind"),
                "flow_id": batch.get("flow_id"),
                "interaction_id": batch.get("interaction_id"),
            }
    return index


def _claim_attachment_payload(
    attachment: ArchivoAdjunto,
    *,
    evidence: Mapping[str, Any] | None = None,
    fallback_source: str = "claim_attachment",
) -> dict[str, Any]:
    metadata = evidence if isinstance(evidence, Mapping) else {}
    payload = serialize_attachment_for_delivery(attachment)
    mime_type = str(payload.get("mimeType") or "")
    payload.update(
        {
            "kind": metadata.get("kind") or ("image" if mime_type.startswith("image/") else "file"),
            "source": metadata.get("source") or fallback_source,
            "origin": metadata.get("origin") or fallback_source,
            "status": metadata.get("status") or "ready",
        }
    )
    for key in ("flow_id", "interaction_id"):
        if metadata.get(key) is not None:
            payload[key] = metadata.get(key)
    _strip_public_storage_fields(payload)
    return payload


def _strip_public_storage_fields(payload: dict[str, Any]) -> None:
    """Keep storage references internal while exposing signed delivery URLs."""

    payload.pop("storage_url", None)
    payload.pop("thumb_storage_url", None)


def _claim_attachments(
    ticket: MunicipioTicket,
    evidence_index: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if getattr(ticket, "foto_url_directa", None):
        legacy = serialize_attachment_for_delivery(
            {
                "id": f"legacy-photo-{ticket.id}",
                "name": "Foto adjunta",
                "url": ticket.foto_url_directa,
            }
        )
        legacy.update(
            {
                "kind": "image",
                "source": "claim_attachment",
                "origin": "claim_attachment",
                "status": "ready",
            }
        )
        _strip_public_storage_fields(legacy)
        attachments.append(legacy)
    rows = (
        ArchivoAdjunto.query.filter_by(municipio_ticket_id=ticket.id)
        .order_by(ArchivoAdjunto.fecha.asc(), ArchivoAdjunto.id.asc())
        .all()
    )
    attachments.extend(
        _claim_attachment_payload(
            attachment,
            evidence=evidence_index.get(attachment.id),
        )
        for attachment in rows
    )
    return attachments


def _tenant_live_chat_status(tenant: TenantProfile | None) -> dict[str, Any]:
    status = build_tenant_live_chat_status(tenant)
    status.setdefault("fallback_mode", "http_chat")
    return status


def _claim_admin_crm_route(ticket_id: int | None, *, focus: str = "live_chat") -> str:
    base_route = "/perfil?tab=tickets"
    if not ticket_id:
        return base_route
    return (
        f"{base_route}&ticket_id={ticket_id}"
        f"&focus={focus}&source=public_tracking&source_model=MunicipioTicket"
    )


def _claim_helpdesk_queue_state(
    conversation: list[dict[str, Any]],
    *,
    mode: str,
    schedule_label: str | None,
) -> dict[str, Any]:
    """Derive the pending operator queue from public conversation comments."""

    latest_team_index = -1
    latest_customer_message: dict[str, Any] | None = None
    pending_customer_messages: list[dict[str, Any]] = []

    for index, item in enumerate(conversation):
        if not isinstance(item, dict):
            continue
        author = str(item.get("author") or "").strip().lower()
        if author in {"team", "admin", "agent", "municipio", "pyme"}:
            latest_team_index = index
            pending_customer_messages.clear()
            continue
        if author != "customer":
            continue
        latest_customer_message = item
        if index > latest_team_index:
            pending_customer_messages.append(item)

    pending_count = len(pending_customer_messages)
    has_pending = pending_count > 0
    state = "pending_admin_response" if has_pending else "up_to_date"
    if has_pending and mode == "live":
        state = "live_agent_attention_needed"
    elif has_pending:
        state = "offline_waiting_admin_response"

    return {
        "contract_version": "claim.helpdesk_queue.v1",
        "state": state,
        "has_pending_customer_message": has_pending,
        "pending_customer_messages": pending_count,
        "latest_customer_message": (
            {
                "id": latest_customer_message.get("id"),
                "created_at": latest_customer_message.get("created_at"),
                "source": latest_customer_message.get("source"),
                "preview": str(latest_customer_message.get("message") or "")[:180],
            }
            if latest_customer_message
            else None
        ),
        "pending_since": (
            pending_customer_messages[0].get("created_at")
            if pending_customer_messages
            else None
        ),
        "sla_target_minutes": 30 if mode == "live" else 240,
        "next_team_action": (
            "reply_from_admin_inbox" if has_pending else "monitor_ticket"
        ),
        "next_team_action_label": (
            "Responder desde la bandeja de reclamos"
            if has_pending
            else "Sin respuesta pendiente"
        ),
        "customer_visible_label": (
            "Tu mensaje quedo pendiente para el equipo"
            if has_pending
            else "El equipo esta al dia con este reclamo"
        ),
        "schedule_label": schedule_label,
    }


def _claim_support_contract(
    ticket: MunicipioTicket,
    tenant: TenantProfile | None,
    *,
    code: str,
    conversation: list[dict[str, Any]],
) -> dict[str, Any]:
    ticket_id = getattr(ticket, "id", None)
    socket_room = build_ticket_room("municipio", ticket_id) if ticket_id else None
    live_chat = _tenant_live_chat_status(tenant)
    if ticket_id:
        live_chat = attach_ticket_room_access(
            live_chat,
            ticket_type="municipio",
            ticket_id=ticket_id,
        )
    available = bool(live_chat.get("enabled") and live_chat.get("available"))
    mode = "live" if available else "offline"
    public_endpoint = f"/api/public/tracking/claims/{ticket_id}/messages" if ticket_id else None
    timeline_endpoint = f"/tickets/municipio/{ticket_id}/timeline" if ticket_id else None
    municipio_id = getattr(ticket, "municipio_id", None)
    schedule_label = live_chat.get("description") or (
        f"{live_chat.get('start_time')} a {live_chat.get('end_time')}"
        if live_chat.get("start_time") and live_chat.get("end_time")
        else None
    )
    primary_cta_label = "Chatear con un agente" if available else "Dejar mensaje para el equipo"
    primary_cta_action = "socket_live_message" if available else "queue_ticket_comment"
    primary_cta_id = "open_live_chat" if available else "leave_offline_message"
    primary_action_variant = "primary" if available else "secondary"
    support_action_label = "Chatear con un agente" if available else "Dejar mensaje"
    admin_focus = "live_chat" if available else "offline_message"
    admin_crm_route = _claim_admin_crm_route(ticket_id, focus=admin_focus)
    queue_state = _claim_helpdesk_queue_state(
        conversation,
        mode=mode,
        schedule_label=schedule_label,
    )
    sla_target_minutes = queue_state.get("sla_target_minutes")
    polling_interval_ms = 10000 if available else 30000
    response_expectation_label = (
        f"Respuesta esperada en hasta {sla_target_minutes} min"
        if available and sla_target_minutes
        else f"El equipo lo ve en el CRM. SLA objetivo {sla_target_minutes} min"
        if sla_target_minutes
        else "El equipo responde desde el CRM del tenant"
    )
    channel_binding_label = "Canal interno del ticket"
    polling_label = f"Actualizacion cada {int(polling_interval_ms / 1000)}s"
    has_customer_activity = bool(queue_state["has_pending_customer_message"])
    crm_writebacks = [
        "public_comment_created",
        "ticket_timeline_updated",
        "admin_inbox_unread_incremented",
    ]
    return {
        "contract_version": "tracking.support.v1",
        "enabled": True,
        "mode": mode,
        "live_chat": live_chat,
        "availability": {
            "state": "online" if available else "offline_accepting_messages",
            "label": "Atencion en vivo disponible" if available else "Mesa de ayuda offline",
            "description": (
                "Un agente puede ver este mensaje en tiempo real."
                if available
                else "Tu mensaje queda asociado al reclamo para que el equipo lo responda en horario administrativo."
            ),
        },
        "cta": {
            "primary": {
                "id": primary_cta_id,
                "label": primary_cta_label,
                "action": primary_cta_action,
                "endpoint": public_endpoint,
                "method": "POST",
                "mode": mode,
                "requires": ["pin", "comentario"],
                "variant": primary_action_variant,
                "safe_for_offline": True,
                "bound_resource": "municipio_ticket",
            },
            "schedule": {
                "id": "view_live_chat_schedule",
                "label": "Ver horario de atencion",
                "schedule_label": schedule_label,
                "timezone": live_chat.get("timezone"),
            },
            "admin": {
                "id": "open_admin_ticket_thread",
                "label": "Responder desde CRM",
                "action": "open_crm_ticket_thread",
                "href": admin_crm_route,
                "frontend_path": admin_crm_route,
                "focus": admin_focus,
                "method": "GET",
                "bound_resource": "municipio_ticket",
                "requires_role": ["admin", "empleado", "super_admin"],
            },
        },
        "service_window": {
            "mode": mode,
            "live_available": available,
            "accepts_messages": True,
            "offline_queue_enabled": not available,
            "admin_configurable": True,
            "tenant_schedule_source": live_chat.get("source"),
            "schedule_label": schedule_label,
            "timezone": live_chat.get("timezone"),
            "outside_hours_mode": "offline_message",
            "next_action": "socket_live_message" if available else "queue_ticket_comment",
            "response_expectation_label": response_expectation_label,
            "channel_binding_label": channel_binding_label,
            "admin_crm_route": admin_crm_route,
        },
        "ticket": {
            "id": ticket_id,
            "type": "municipio",
            "code": code,
            "requires_pin": True,
            "pin_transport": "query_param",
        },
        "conversation": {
            "id": f"municipio-ticket-{ticket_id}" if ticket_id else None,
            "messages": conversation,
            "message_count": len(conversation),
            "public_messages_visible": True,
            "admin_surface": "tenant_claims_inbox",
            "unread_for_team": has_customer_activity,
            "writebacks": crm_writebacks,
        },
        "endpoints": {
            "send_message": public_endpoint,
            "timeline": timeline_endpoint,
        },
        "socket": {
            "enabled": bool(available and socket_room and live_chat.get("access_token")),
            "event": "new_chat_message",
            "room": socket_room,
            "access_token": live_chat.get("access_token"),
            "access_mode": live_chat.get("access_mode"),
            "requires_auth": True,
            "fallback_transport": "http_polling",
        },
        "polling": {
            "enabled": True,
            "interval_ms": polling_interval_ms,
            "endpoint": timeline_endpoint,
        },
        "webview_policy": {
            "stay_inside_tracking": True,
            "external_redirect_required": False,
            "pin_required_for_public_reply": True,
            "safe_for_whatsapp_cta": True,
            "admin_handoff_inside_crm": True,
        },
        "admin_response_surface": {
            "id": "tenant_claims_inbox",
            "label": "Inbox de reclamos",
            "endpoint": "/api/v2/inbox/omnichannel",
            "thread_binding": "municipio_ticket_id",
            "socket_room": socket_room,
            "route": admin_crm_route,
            "href": admin_crm_route,
            "frontend_path": admin_crm_route,
            "focus": admin_focus,
            "ticket_id": ticket_id,
            "unread_counter_key": "claim_public_messages",
            "writebacks": crm_writebacks,
            "actions": [
                {
                    "id": "open_admin_ticket_thread",
                    "label": "Abrir hilo del reclamo",
                    "href": admin_crm_route,
                    "frontend_path": admin_crm_route,
                    "ui_hint": "focus_ticket_conversation",
                },
                {
                    "id": "reply_from_claim_inbox",
                    "label": "Responder al vecino",
                    "href": admin_crm_route,
                    "frontend_path": admin_crm_route,
                    "ui_hint": "focus_reply_composer",
                },
            ],
        },
        "operator_queue": {
            **queue_state,
            "id": "claim_helpdesk_queue",
            "label": "Cola de mesa de ayuda",
            "unread_on_customer_message": True,
            "requires_admin_response": has_customer_activity,
            "crm_writebacks": crm_writebacks,
            "routing_key": f"municipio:{municipio_id}:ticket:{ticket_id}" if municipio_id and ticket_id else None,
            "admin_route": admin_crm_route,
            "admin_action": "reply_from_claim_inbox",
        },
        "ui": {
            "render_as": "ticket_bound_helpdesk",
            "primary_cta": primary_cta_label,
            "primary_action": primary_cta_action,
            "primary_action_variant": primary_action_variant,
            "support_action_label": support_action_label,
            "live_label": "Chat en vivo",
            "offline_label": "Dejar mensaje",
            "schedule_label": schedule_label,
            "empty_state": "Todavia no hay mensajes publicos en este reclamo.",
            "team_unread_label": "Queda como no leido para el equipo",
            "queue_state_label": queue_state["customer_visible_label"],
            "next_team_action_label": queue_state["next_team_action_label"],
            "response_expectation_label": response_expectation_label,
            "channel_binding_label": channel_binding_label,
            "polling_label": polling_label,
            "no_external_redirect_label": "Sin redireccion externa",
            "operational_state_label": "Canal seguro asociado al reclamo",
            "admin_route_label": "Abrir hilo en CRM",
        },
    }


def _legacy_order_items(details: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(details or "[]")
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    items = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "title": item.get("nombre") or item.get("title") or item.get("producto"),
                "quantity": item.get("cantidad") or item.get("quantity") or 1,
                "unit_price": _as_float(item.get("precio") or item.get("unit_price")),
                "image_url": item.get("imagen_url") or item.get("image_url"),
            }
        )
    return items


def _public_assisted_items(order: PedidoConversacional, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    if metadata.get("contract_version") != "marketplace.assisted_request.v1":
        return order.items or []

    crm_order_draft = metadata.get("crm_order_draft") if isinstance(metadata.get("crm_order_draft"), dict) else {}
    draft_lines = crm_order_draft.get("lines") if isinstance(crm_order_draft.get("lines"), list) else []
    if draft_lines:
        items: list[dict[str, Any]] = []
        for index, line in enumerate(draft_lines):
            if not isinstance(line, dict):
                continue
            catalog_match = line.get("catalog_match") if isinstance(line.get("catalog_match"), dict) else {}
            status = str(line.get("status") or "").strip().lower()
            matched = bool(
                status == "catalog_matched"
                or line.get("catalog_item_id")
                or line.get("catalogo_item_id")
                or catalog_match.get("id")
                or catalog_match.get("catalog_item_id")
            ) and line.get("needs_operator_review") is not True
            title = (
                line.get("source_name")
                or line.get("nombre")
                or line.get("name")
                or catalog_match.get("nombre")
                or catalog_match.get("name")
                or line.get("sku")
                or "Item detectado"
            )
            items.append(
                {
                    "id": (
                        line.get("catalog_item_id")
                        or line.get("catalogo_item_id")
                        or catalog_match.get("id")
                        or line.get("line_id")
                        or f"draft-{index + 1}"
                    ),
                    "title": title,
                    "quantity": line.get("quantity") or line.get("cantidad") or 1,
                    "status": "matched_catalog" if matched else "operator_review",
                }
            )
        if items:
            return items

    raw_payload = order.items[0] if order.items and isinstance(order.items[0], dict) else {}
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_payload.get("items_detectados") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("nombre") or item.get("title") or item.get("name") or item.get("sku") or "Item detectado"
        items.append(
            {
                "id": item.get("catalogo_item_id") or f"detected-{index + 1}",
                "title": title,
                "quantity": item.get("cantidad") or item.get("quantity") or 1,
                "status": "matched_catalog",
            }
        )

    for index, label in enumerate(raw_payload.get("no_encontrados_labels") or []):
        items.append(
            {
                "id": f"review-{index + 1}",
                "title": str(label),
                "quantity": 1,
                "status": "operator_review",
            }
        )

    if not items:
        label = metadata.get("request_kind_label") or raw_payload.get("request_kind_label") or "Solicitud recibida"
        items.append({"id": f"assisted-{order.id}", "title": label, "quantity": 1, "status": "operator_review"})
    return items


def _assisted_tracking_info(metadata: dict[str, Any]) -> dict[str, Any]:
    follow_up = metadata.get("public_follow_up") if isinstance(metadata.get("public_follow_up"), dict) else {}
    tracking = follow_up.get("tracking") if isinstance(follow_up.get("tracking"), dict) else {}
    return tracking if isinstance(tracking, dict) else {}


def _is_marketplace_assisted_order(order: Any) -> bool:
    if not isinstance(order, PedidoConversacional):
        return False
    metadata = order.metadata_payload if isinstance(order.metadata_payload, dict) else {}
    return metadata.get("contract_version") == "marketplace.assisted_request.v1"


def _stored_assisted_tracking_token(order: Any) -> str | None:
    if not _is_marketplace_assisted_order(order):
        return None
    metadata = order.metadata_payload if isinstance(order.metadata_payload, dict) else {}
    tracking = _assisted_tracking_info(metadata)
    token = str(
        tracking.get("token")
        or tracking.get("access_token")
        or metadata.get("tracking_token")
        or ""
    ).strip()
    return token or None


def _legacy_tracking_secret() -> bytes | None:
    if not has_app_context():
        return None
    configured = current_app.config.get("ORDER_TRACKING_TOKEN_SECRET")
    secret = str(configured or current_app.config.get("SECRET_KEY") or "").strip()
    return secret.encode("utf-8") if secret else None


def _legacy_tracking_subject(order: Any) -> str | None:
    order_id = getattr(order, "id", None)
    if order_id is None:
        return None
    public_code = (
        getattr(order, "nro_pedido", None)
        or getattr(order, "public_code", None)
        or f"{order.__class__.__name__}:{order_id}"
    )
    tenant_id = getattr(order, "tenant_id", None) or getattr(order, "pyme_id", None) or ""
    return "|".join(
        (
            "order-tracking",
            ORDER_TRACKING_TOKEN_VERSION,
            order.__class__.__name__,
            str(order_id),
            str(tenant_id),
            str(public_code),
        )
    )


def issue_order_tracking_token(order: Any) -> str | None:
    """Return the opaque capability token expected for an order tracking link."""

    if _is_marketplace_assisted_order(order):
        return _stored_assisted_tracking_token(order)

    secret = _legacy_tracking_secret()
    subject = _legacy_tracking_subject(order)
    if not secret or not subject:
        return None
    digest = hmac.new(secret, subject.encode("utf-8"), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{ORDER_TRACKING_TOKEN_VERSION}.{encoded}"


def order_supports_redacted_tracking(order: Any) -> bool:
    """Only non-enumerable legacy order codes retain a tokenless status view."""

    return isinstance(order, PymePedido)


def validate_order_tracking_access(order: Any, token: str | None) -> tuple[bool, str | None]:
    """Validate public access without treating an order identifier as a credential."""

    expected_token = str(issue_order_tracking_token(order) or "").strip()
    provided_token = str(token or "").strip()

    if not expected_token:
        return False, "tracking_token_missing"
    if not provided_token:
        return False, "tracking_token_required"
    if not secrets.compare_digest(expected_token, provided_token):
        return False, "tracking_token_invalid"
    return True, None


def _order_snapshot(order: Any) -> dict[str, Any]:
    if isinstance(order, PymePedido):
        return {
            "id": f"legacy:{order.id}",
            "source_model": "PymePedido",
            "source_id": order.id,
            "legacy_number": order.nro_pedido,
            "tenant_id": order.tenant_id,
            "status": order.estado,
            "channel": "whatsapp",
            "contact": {
                "name": order.nombre_cliente,
                "email": order.email_cliente,
                "phone": order.telefono_cliente,
            },
            "totals": {
                "monetary": _as_float(order.monto_total) or 0.0,
                "points": 0,
                "currency": getattr(order, "moneda", None) or "ARS",
            },
            "items": _legacy_order_items(order.detalles),
            "metadata": {"direccion": order.direccion, "pyme_id": order.pyme_id},
            "created_at": _iso(order.fecha),
            "updated_at": _iso(order.fecha),
        }

    if isinstance(order, MarketOrder):
        items = []
        for item in getattr(order, "items", []) or []:
            items.append(
                {
                    "id": item.id,
                    "product_id": item.product_id,
                    "title": item.name_snapshot,
                    "quantity": item.quantity,
                    "unit_price": _as_float(item.price_monetary),
                    "points": item.price_points,
                    "currency": item.currency,
                    "modalidad": item.modalidad,
                }
            )
        return {
            "id": f"market:{order.id}",
            "source_model": "MarketOrder",
            "source_id": order.id,
            "tenant_id": order.tenant_id,
            "status": order.status,
            "channel": order.channel or "web",
            "contact": {
                "name": order.contact_name,
                "phone": order.contact_phone,
            },
            "totals": {
                "monetary": _as_float(order.total_monetary) or 0.0,
                "points": order.total_points or 0,
                "currency": order.currency or "ARS",
            },
            "items": items,
            "metadata": order.metadata_payload if isinstance(order.metadata_payload, dict) else {},
            "created_at": _iso(order.created_at),
            "updated_at": _iso(order.updated_at),
        }

    if isinstance(order, PedidoConversacional):
        metadata = order.metadata_payload if isinstance(order.metadata_payload, dict) else {}
        tracking = _assisted_tracking_info(metadata)
        contact = metadata.get("contact") if isinstance(metadata.get("contact"), dict) else {}
        contacto = metadata.get("contacto") if isinstance(metadata.get("contacto"), dict) else {}
        effective_contact = contact or {
            "name": contacto.get("nombre"),
            "email": contacto.get("email"),
            "phone": contacto.get("telefono"),
        }
        return {
            "id": f"conversational:{order.id}",
            "source_model": "PedidoConversacional",
            "source_id": order.id,
            "legacy_number": tracking.get("code") or f"pc-{order.id}",
            "tracking": tracking,
            "tenant_id": order.tenant_id,
            "status": order.estado,
            "channel": order.origen or "whatsapp",
            "contact": {
                "name": effective_contact.get("name") or effective_contact.get("nombre"),
                "email": effective_contact.get("email"),
                "phone": effective_contact.get("phone") or effective_contact.get("telefono"),
            },
            "totals": {
                "monetary": _as_float(order.monto_monetario) or 0.0,
                "points": order.monto_puntos or 0,
                "currency": metadata.get("currency") or "ARS",
            },
            "items": _public_assisted_items(order, metadata),
            "metadata": metadata,
            "created_at": _iso(order.created_at),
            "updated_at": _iso(order.updated_at),
        }

    return {
        "id": str(getattr(order, "id", "")),
        "source_model": order.__class__.__name__,
        "source_id": getattr(order, "id", None),
        "tenant_id": getattr(order, "tenant_id", None),
        "status": getattr(order, "status", None) or getattr(order, "estado", None),
        "channel": getattr(order, "channel", None),
        "contact": {},
        "totals": {},
        "items": [],
        "metadata": {},
        "created_at": _iso(getattr(order, "created_at", None) or getattr(order, "fecha", None)),
        "updated_at": _iso(getattr(order, "updated_at", None) or getattr(order, "fecha", None)),
    }


def build_claim_tracking_experience(ticket: MunicipioTicket, tenant: TenantProfile | None = None) -> dict[str, Any]:
    location = {
        "address": getattr(ticket, "direccion", None),
        "district": getattr(ticket, "distrito", None),
        "lat": getattr(ticket, "latitud", None),
        "lng": getattr(ticket, "longitud", None),
    }
    timeline = [
        {
            "id": f"claim-created-{ticket.id}",
            "type": "claim.created",
            "label": "Reclamo recibido",
            "message": getattr(ticket, "asunto", None) or getattr(ticket, "categoria", None),
            "created_at": _iso(getattr(ticket, "fecha", None)),
        },
        {
            "id": f"claim-status-{ticket.id}",
            "type": "claim.status",
            "label": "Estado actual",
            "status": getattr(ticket, "estado", None),
            "created_at": _iso(getattr(ticket, "ultima_actividad", None) or getattr(ticket, "fecha", None)),
        },
    ]
    evidence_index = _claim_evidence_index(ticket)
    conversation = _comment_timeline(
        getattr(ticket, "comentarios", None),
        evidence_index=evidence_index,
    )
    timeline.extend(conversation)
    attachments = _claim_attachments(ticket, evidence_index)

    code = str(getattr(ticket, "nro_ticket", "") or "")
    display_code = code if code.upper().startswith(("M-", "S-")) else f"M-{code}"
    pin = str(getattr(ticket, "consulta_pin", "") or "")
    tracking_page_url = f"/tracking/claim/{code}"
    if pin:
        tracking_page_url = f"{tracking_page_url}#pin={pin}"
    support = _claim_support_contract(ticket, tenant, code=display_code, conversation=conversation)
    support_primary_cta = (support.get("cta") or {}).get("primary") or {}
    return {
        "contract_version": TRACKING_EXPERIENCE_CONTRACT_VERSION,
        "kind": "claim",
        "tenant": _tenant_ref(tenant),
        "resource": {
            "id": ticket.id,
            "code": display_code,
            "category": getattr(ticket, "categoria", None),
            "subject": getattr(ticket, "asunto", None) or getattr(ticket, "categoria", None),
            "channel": getattr(ticket, "canal_ingreso", None),
            "created_at": _iso(getattr(ticket, "fecha", None)),
            "updated_at": _iso(getattr(ticket, "ultima_actividad", None)),
        },
        "status": _stage_payload(getattr(ticket, "estado", None), kind="claim"),
        "location": location,
        "map": _tracking_map(location),
        "attachments": attachments,
        "timeline": [item for item in timeline if item.get("created_at") or item.get("message") or item.get("status")],
        "support": support,
        "actions": [
            {
                "id": "send_message",
                "label": support_primary_cta.get("label") or "Enviar mensaje",
                "endpoint": f"/api/public/tracking/claims/{ticket.id}/messages",
                "requires": ["pin", "comentario"],
                "mode": support.get("mode"),
                "action": support_primary_cta.get("action"),
                "safe_for_offline": True,
                "bound_resource": "municipio_ticket",
            },
            {
                "id": "open_tracking_page",
                "label": "Abrir seguimiento",
                "url": tracking_page_url,
                "requires_pin": bool(pin),
            },
        ],
        "frontend_contract": {
            "render_as": "tracking_map_timeline_helpdesk",
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "timeline_only_when_no_coordinates",
            "support_component": "ticket_bound_helpdesk",
        },
    }


def build_tenant_claim_tracking_experience(
    ticket: TenantTicket,
    tenant: TenantProfile | None = None,
) -> dict[str, Any]:
    """Build the isolated, read-only tracking view for receipt-backed claims.

    ``TenantTicket`` has no public comment surface.  In particular, this
    builder must never serialize ``datos_extra`` wholesale or echo any intake
    credential/hash into the response.
    """

    code = f"T-{ticket.id}"
    location = {
        "address": None,
        "district": None,
        "lat": ticket.latitud,
        "lng": ticket.longitud,
    }
    created_at = _iso(ticket.created_at)
    updated_at = _iso(ticket.updated_at) or created_at
    timeline = [
        {
            "id": f"tenant-claim-created-{ticket.id}",
            "type": "claim.created",
            "label": "Reclamo recibido",
            "created_at": created_at,
        },
        {
            "id": f"tenant-claim-status-{ticket.id}",
            "type": "claim.status",
            "label": "Estado actual",
            "status": ticket.estado,
            "created_at": updated_at,
        },
    ]
    return {
        "contract_version": TRACKING_EXPERIENCE_CONTRACT_VERSION,
        "kind": "claim",
        "tenant": _tenant_ref(tenant),
        "resource": {
            "id": ticket.id,
            "code": code,
            "source_model": "TenantTicket",
            "category": ticket.categoria,
            "subject": ticket.categoria or "Reclamo",
            "channel": ticket.origen,
            "created_at": created_at,
            "updated_at": updated_at,
        },
        "status": _stage_payload(ticket.estado, kind="claim"),
        "location": location,
        "map": _tracking_map(location),
        "attachments": [],
        "timeline": timeline,
        "support": {
            "enabled": False,
            "mode": "disabled",
            "reason_code": "tenant_claim_messages_not_supported",
            "conversation": {"enabled": False, "messages": []},
        },
        "actions": [
            {
                "id": "open_tracking_page",
                "label": "Abrir seguimiento",
                "url": f"/tracking/claim/{code}",
                "requires_pin": True,
                "credential_transport": "x-tracking-pin-header",
            }
        ],
        "frontend_contract": {
            "render_as": "tracking_map_timeline",
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "timeline_only_when_no_coordinates",
            "support_component": None,
        },
    }


def build_order_tracking_experience(
    order: Any,
    tenant: TenantProfile | None = None,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    serialized = _order_snapshot(order)
    source = serialized.get("source_model")
    status = serialized.get("status")
    items = serialized.get("items") or []
    tracking = serialized.get("tracking") if isinstance(serialized.get("tracking"), dict) else {}
    code = serialized.get("legacy_number") or serialized.get("id") or str(getattr(order, "id", ""))
    tracking_url = _tracking_path_without_query_secret(
        tracking.get("path") or f"/tracking/order/{code}"
    )
    location = {
        "address": getattr(order, "direccion", None) or (serialized.get("metadata") or {}).get("direccion"),
        "lat": getattr(order, "latitud", None),
        "lng": getattr(order, "longitud", None),
    }
    if source == "PymePedido" and not items:
        items = _legacy_order_items(getattr(order, "detalles", None))
    if not include_private:
        items = []
        location = {"address": None, "lat": None, "lng": None}

    customer = serialized.get("contact") or {}
    totals = serialized.get("totals") or {}
    if not include_private:
        customer = {"name": None, "email": None, "phone": None}
        totals = {"monetary": None, "points": None, "currency": None}

    public_resource_id = str(serialized.get("id") or code) if include_private else str(code)
    access_mode = "redacted_public_status"
    if include_private:
        access_mode = tracking.get("access") or "signed_token"

    timeline = [
        {
            "id": f"order-created-{public_resource_id}",
            "type": "order.created",
            "label": "Pedido recibido",
            "created_at": serialized.get("created_at"),
        },
        {
            "id": f"order-status-{public_resource_id}",
            "type": "order.status",
            "label": "Estado actual",
            "status": status,
            "created_at": serialized.get("updated_at") or serialized.get("created_at"),
        },
    ]
    if isinstance(order, MarketOrder) and hasattr(order, "events"):
        events = order.events.order_by(OrderEvent.created_at.asc()).limit(20).all()
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            timeline.append(
                {
                    "id": f"event-{event.id}",
                    "type": event.type,
                    "label": payload.get("message") or event.type,
                    "status": payload.get("status"),
                    "created_at": _iso(event.created_at),
                }
            )

    return {
        "contract_version": TRACKING_EXPERIENCE_CONTRACT_VERSION,
        "kind": "order",
        "tenant": _tenant_ref(tenant),
        "resource": {
            "id": public_resource_id,
            "code": code,
            "source_model": source,
            "channel": serialized.get("channel"),
            "created_at": serialized.get("created_at"),
            "updated_at": serialized.get("updated_at"),
        },
        "status": _stage_payload(status, kind="order"),
        "customer": customer,
        "totals": totals,
        "items": items,
        "location": location,
        "map": _tracking_map(location),
        "timeline": [item for item in timeline if item.get("created_at") or item.get("status") or item.get("label")],
        "actions": [
            {
                "id": "send_message",
                "label": "Enviar mensaje",
                "endpoint": "/tracking/api/send-message",
                "requires_token": True,
                "enabled": bool(include_private),
            },
            {
                "id": "open_tracking_page",
                "label": "Abrir seguimiento",
                "url": tracking_url,
                "requires_token": True,
            },
        ],
        "privacy": {
            "authorized": bool(include_private),
            "pii_redacted": not include_private,
            "access_level": "full" if include_private else "redacted",
            "proof_required": True,
            "redacted_fields": (
                []
                if include_private
                else ["customer", "location", "coordinates", "items", "totals", "conversation"]
            ),
        },
        "frontend_contract": {
            "render_as": "tracking_map_timeline",
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "timeline_only_when_no_coordinates",
            "access": access_mode,
        },
    }


def resolve_order_by_code(code: str):
    normalized = (code or "").strip()
    if not normalized:
        return None
    pedido = PymePedido.query.filter_by(nro_pedido=normalized).first()
    if pedido:
        return pedido
    lowered = normalized.lower()
    for prefix, model in (("market:", MarketOrder), ("mo-", MarketOrder), ("conversational:", PedidoConversacional), ("pc-", PedidoConversacional)):
        if lowered.startswith(prefix):
            raw_id = lowered.split(prefix, 1)[1]
            if raw_id.isdigit():
                return model.query.get(int(raw_id))
    if normalized.isdigit():
        return MarketOrder.query.get(int(normalized)) or PedidoConversacional.query.get(int(normalized))
    return None


def resolve_tenant_for_order(order: Any) -> TenantProfile | None:
    tenant_id = getattr(order, "tenant_id", None)
    if tenant_id:
        return TenantProfile.query.get(tenant_id)
    pyme_id = getattr(order, "pyme_id", None)
    if pyme_id:
        return TenantProfile.query.filter_by(pyme_id=pyme_id).first()
    return None
