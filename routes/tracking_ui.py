from flask import Blueprint, render_template, request, jsonify, abort, current_app
from models import PymePedido, TenantProfile, db, Order, User, PymeTicket, TicketComentario, MunicipioTicket
from services.pedido_service import servicio_pedidos
from extensions import limiter
import json
import hashlib
from datetime import datetime
from services.ticket_service import servicio_tickets
from socket_service import emit_new_chat_message, emit_ticket_unread_changed
import random
import uuid
from sqlalchemy import func
from services.ticket_realtime_state import build_ticket_collaboration_state
from services.tracking_experience import (
    TRACKING_EXPERIENCE_CONTRACT_VERSION,
    build_claim_tracking_experience,
    build_order_tracking_experience,
    resolve_order_by_code,
    resolve_tenant_for_order,
    validate_order_tracking_access,
)

tracking_ui_bp = Blueprint('tracking_ui_bp', __name__)


_DEFAULT_TRACKING_FAILURE_LIMIT = 5
_DEFAULT_TRACKING_FAILURE_WINDOW_SECONDS = 60
_DEFAULT_TRACKING_SUBJECT_FAILURE_LIMIT = 20


def _tracking_pin(payload: dict | None = None) -> str:
    """Read public tracking credentials without requiring URL query secrets."""

    body = payload if isinstance(payload, dict) else {}
    return str(
        request.headers.get("X-Tracking-Pin")
        or request.headers.get("pin")
        or body.get("pin")
        or request.args.get("pin")
        or ""
    ).strip()


def _tracking_access_token(payload: dict | None = None) -> str:
    body = payload if isinstance(payload, dict) else {}
    return str(
        request.headers.get("X-Tracking-Token")
        or body.get("tracking_token")
        or body.get("access_token")
        or request.args.get("token")
        or request.args.get("access_token")
        or ""
    ).strip()


def _positive_config_int(name: str, default: int) -> int:
    try:
        value = int(current_app.config.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _tracking_failure_rate_limit() -> str:
    configured = current_app.config.get("TRACKING_FAILURE_RATE_LIMIT")
    if configured:
        return str(configured)
    limit = _positive_config_int(
        "TRACKING_FAILURE_RATE_LIMIT_ATTEMPTS",
        _DEFAULT_TRACKING_FAILURE_LIMIT,
    )
    window = _positive_config_int(
        "TRACKING_FAILURE_RATE_LIMIT_WINDOW_SECONDS",
        _DEFAULT_TRACKING_FAILURE_WINDOW_SECONDS,
    )
    return f"{limit} per {window} seconds"


def _tracking_subject_failure_rate_limit() -> str:
    configured = current_app.config.get("TRACKING_SUBJECT_FAILURE_RATE_LIMIT")
    if configured:
        return str(configured)
    limit = _positive_config_int(
        "TRACKING_SUBJECT_FAILURE_RATE_LIMIT_ATTEMPTS",
        _DEFAULT_TRACKING_SUBJECT_FAILURE_LIMIT,
    )
    window = _positive_config_int(
        "TRACKING_FAILURE_RATE_LIMIT_WINDOW_SECONDS",
        _DEFAULT_TRACKING_FAILURE_WINDOW_SECONDS,
    )
    return f"{limit} per {window} seconds"


def _tracking_failure_subject_key() -> str:
    payload = request.get_json(silent=True) if request.method != "GET" else None
    payload = payload if isinstance(payload, dict) else {}
    view_args = request.view_args or {}
    kind = str(
        request.args.get("kind")
        or request.args.get("type")
        or payload.get("kind")
        or "claim"
    ).strip().lower()
    kind = {"reclamo": "claim", "pedido": "order"}.get(kind, kind)
    ticket_reference = (
        view_args.get("ticket_id")
        or view_args.get("nro_ticket")
        or request.args.get("code")
        or request.args.get("nro_ticket")
        or request.args.get("nro_pedido")
        or request.args.get("order_id")
        or payload.get("ticket_id")
        or payload.get("nro_ticket")
        or payload.get("nro_pedido")
    )
    pin = _tracking_pin(payload)

    # Group attempts by stable ticket when possible so rotating PIN guesses
    # cannot create fresh buckets. The PIN fingerprint is only a fallback.
    if ticket_reference is not None and str(ticket_reference).strip():
        normalized_reference = str(ticket_reference).strip().lower()
        if kind == "claim" and normalized_reference.upper().startswith(("M-", "S-")):
            normalized_reference = normalized_reference[2:].strip()
        subject = f"{kind}:ticket:{normalized_reference}"
    elif pin is not None and str(pin).strip():
        pin_fingerprint = hashlib.sha256(str(pin).strip().encode("utf-8")).hexdigest()
        subject = f"{kind}:pin:{pin_fingerprint}"
    else:
        subject = f"{kind}:unknown"

    subject_fingerprint = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    return f"tracking-subject-failure:{subject_fingerprint}"


def _tracking_failure_rate_key() -> str:
    client_ip = request.remote_addr or "0.0.0.0"
    return f"tracking-ip-failure:{client_ip}:{_tracking_failure_subject_key()}"


def _deduct_tracking_failure(response) -> bool:
    return response.status_code in {403, 404}


def _tracking_failure_rate_limited(_request_limit):
    response = _tracking_error(
        "Demasiados intentos fallidos. Intenta nuevamente mas tarde.",
        429,
        "tracking_rate_limited",
        "retry_later",
        retryable=True,
    )
    response.headers["Retry-After"] = str(
        _positive_config_int(
            "TRACKING_FAILURE_RATE_LIMIT_WINDOW_SECONDS",
            _DEFAULT_TRACKING_FAILURE_WINDOW_SECONDS,
        )
    )
    return response


@tracking_ui_bp.errorhandler(429)
def _handle_tracking_rate_limit(error):
    embedded_response = getattr(error, "response", None)
    if embedded_response is not None:
        return embedded_response
    return _tracking_failure_rate_limited(None)


def _tracking_request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _tracking_json(payload: dict, status: int = 200):
    request_id = _tracking_request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _tracking_error(
    message: str,
    status_code: int,
    reason_code: str,
    action_hint: str,
    *,
    retryable: bool = False,
):
    return _tracking_json(
        {
            "contract_version": TRACKING_EXPERIENCE_CONTRACT_VERSION,
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": retryable,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
        },
        status_code,
    )


def _build_public_claim_unread_payload(
    ticket: MunicipioTicket,
    comment: TicketComentario,
    tenant: TenantProfile | None = None,
) -> dict:
    """Payload focused on admin inbox reconciliation for public tracking replies."""

    try:
        collaboration_state = build_ticket_collaboration_state(
            ticket_type="municipio",
            ticket_id=ticket.id,
        )
    except Exception:
        collaboration_state = {}

    latest_comment_id = comment.id or collaboration_state.get("latest_comment_id")
    unread_count = max(int(collaboration_state.get("unread_count") or 0), 1)
    unread_viewer_count = max(int(collaboration_state.get("unread_viewer_count") or 0), 1)
    collaboration_state = {
        **collaboration_state,
        "latest_comment_id": latest_comment_id,
        "unread_count": unread_count,
        "has_unread": True,
        "unread_viewer_count": unread_viewer_count,
        "operational_status": "attention_needed",
    }

    municipio_id = getattr(ticket, "municipio_id", None)
    socket_room = f"municipio_{municipio_id}" if municipio_id else None
    return {
        "ticket_id": ticket.id,
        "ticket_number": getattr(ticket, "nro_ticket", None),
        "comment_id": comment.id,
        "latest_comment_id": latest_comment_id,
        "unread_count": unread_count,
        "has_unread": True,
        "unread_viewer_count": unread_viewer_count,
        "requires_response": True,
        "tipo": "municipio",
        "tenant_type": "municipio",
        "tenant_id": municipio_id,
        "tenant_profile_id": getattr(tenant, "id", None),
        "tenant_slug": getattr(tenant, "slug", None),
        "municipio_id": municipio_id,
        "socket_room": socket_room,
        "source": "public_tracking",
        "summary": {
            "latest_comment_id": latest_comment_id,
            "unread_count": unread_count,
            "has_unread": True,
            "unread_viewer_count": unread_viewer_count,
        },
        "collaboration_state": collaboration_state,
    }


def _resolve_claim_tenant(ticket: MunicipioTicket) -> TenantProfile | None:
    tenant = db.session.get(TenantProfile, ticket.tenant_id) if ticket.tenant_id else None
    if not tenant and ticket.municipio_id:
        tenant = TenantProfile.query.filter_by(municipio_id=ticket.municipio_id).first()
    return tenant


def _normalize_claim_code(code: str | None) -> str:
    normalized = str(code or "").strip()
    prefix_probe = normalized.upper()
    return normalized[2:].strip() if prefix_probe.startswith(("M-", "S-")) else normalized


def _claim_code_candidates(code: str | None) -> list[str]:
    raw = str(code or "").strip()
    if not raw:
        return []
    normalized = _normalize_claim_code(raw)
    candidates: list[str] = []
    for value in (raw, normalized):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _find_claim_by_public_code(code: str | None, pin: str | None = None) -> MunicipioTicket | None:
    candidates = _claim_code_candidates(code)
    if not candidates:
        return None

    base_query = MunicipioTicket.query
    if pin is not None:
        base_query = base_query.filter(MunicipioTicket.consulta_pin == str(pin))

    ticket = base_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
    if ticket:
        return ticket

    lower_candidates = list({candidate.lower() for candidate in candidates if candidate})
    if not lower_candidates:
        return None
    return base_query.filter(func.lower(MunicipioTicket.nro_ticket).in_(lower_candidates)).first()


def _build_public_claim_message_payload(
    ticket: MunicipioTicket,
    comment: TicketComentario,
    tenant: TenantProfile | None,
    previous_status: str | None,
) -> dict:
    tracking_payload = build_claim_tracking_experience(ticket, tenant)
    support = tracking_payload.get("support") or {}
    live_mode = support.get("mode") or "offline"
    unread_payload = _build_public_claim_unread_payload(ticket, comment, tenant)
    admin_surface = support.get("admin_response_surface") or {}
    admin_route = admin_surface.get("frontend_path") or admin_surface.get("href") or admin_surface.get("route") or "/perfil?tab=tickets"
    operator_queue = support.get("operator_queue") or {}
    support_conversation = support.get("conversation") or {}
    crm_writebacks = (
        operator_queue.get("crm_writebacks")
        or admin_surface.get("writebacks")
        or support_conversation.get("writebacks")
        or [
            "public_comment_created",
            "ticket_timeline_updated",
            "admin_inbox_unread_incremented",
        ]
    )
    comment_payload = {
        "id": comment.id,
        "message": comment.comentario,
        "comentario": comment.comentario,
        "author": "customer",
        "source": comment.origen,
        "created_at": comment.fecha.isoformat() if comment.fecha else None,
        "requires_response": True,
        "unread_for_team": True,
    }

    return {
        "contract_version": "tracking.support_message.v1",
        "success": True,
        "status": "ok",
        "message": (
            "Mensaje enviado al canal de atencion en vivo."
            if live_mode == "live"
            else "Mensaje recibido. Queda asociado al reclamo para que el equipo lo responda."
        ),
        "ticket_id": ticket.id,
        "ticket_number": ticket.nro_ticket,
        "comment": comment_payload,
        "chat_entry": {
            "id": comment.id,
            "ticket_id": ticket.id,
            "ticket_number": ticket.nro_ticket,
            "type": "public_tracking_message",
            "comentario": comment.comentario,
            "fecha": comment_payload["created_at"],
            "es_admin": False,
            "autor": "vecino",
            "autor_nombre": ticket.nombre_vecino or "Yo",
            "origen": comment.origen,
            "requires_response": True,
            "unread_for_team": True,
        },
        "delivery": {
            "mode": live_mode,
            "channel": "ticket_bound_helpdesk",
            "realtime_available": live_mode == "live",
            "offline_queue": live_mode != "live",
            "admin_surface": "tenant_claims_inbox",
            "admin_route": admin_route,
            "reply_status": "sent_to_live_chat" if live_mode == "live" else "queued_for_agent",
            "queue_state": operator_queue.get("state") or "pending_admin_response",
            "pending_customer_messages": operator_queue.get("pending_customer_messages") or 1,
            "next_team_action": operator_queue.get("next_team_action") or "reply_from_admin_inbox",
            "admin_unread": True,
            "timeline_updated": True,
            "inbox_increment": True,
            "next_action": (support.get("service_window") or {}).get("next_action"),
        },
        "crm_writeback": {
            "admin_surface": admin_surface.get("id") or "tenant_claims_inbox",
            "route": admin_route,
            "href": admin_route,
            "frontend_path": admin_route,
            "thread_binding": admin_surface.get("thread_binding") or "municipio_ticket_id",
            "ticket_id": ticket.id,
            "ticket_number": ticket.nro_ticket,
            "comment_id": comment.id,
            "unread_for_team": True,
            "requires_admin_response": True,
            "queue_state": operator_queue.get("state") or "pending_admin_response",
            "pending_customer_messages": operator_queue.get("pending_customer_messages") or 1,
            "next_team_action": operator_queue.get("next_team_action") or "reply_from_admin_inbox",
            "timeline_updated": True,
            "inbox_increment": True,
            "writebacks": crm_writebacks,
            "previous_status": previous_status,
            "current_status": ticket.estado,
            "actions": admin_surface.get("actions") or [],
        },
        "unread_event": unread_payload,
        "timeline_endpoint": support.get("endpoints", {}).get("timeline"),
        "tracking": tracking_payload,
    }


def _persist_public_claim_tracking_message(ticket: MunicipioTicket, mensaje: str) -> dict:
    now = datetime.now()
    previous_status = getattr(ticket, "estado", None)
    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=mensaje,
        fecha=now,
        es_admin=False,
        origen="public_tracking",
        user_id=getattr(ticket, "user_id", None),
    )
    db.session.add(comment)
    if hasattr(ticket, "ultima_actividad"):
        ticket.ultima_actividad = now

    if ticket.estado in ["resuelto", "cerrado"]:
        ticket.estado = "abierto"

    db.session.commit()

    tenant = _resolve_claim_tenant(ticket)
    payload = _build_public_claim_message_payload(ticket, comment, tenant, previous_status)
    try:
        if tenant:
            emit_new_chat_message(
                {
                    "tenant_type": "municipio",
                    "tenant_id": tenant.id,
                    "municipio_id": ticket.municipio_id,
                    "ticket_id": ticket.id,
                    "ticket_number": ticket.nro_ticket,
                    "comment_id": comment.id,
                    "requires_response": True,
                    "admin_unread": True,
                    "admin_route": payload.get("crm_writeback", {}).get("frontend_path"),
                    "admin_surface": payload.get("crm_writeback", {}).get("admin_surface"),
                    "message": {
                        "id": comment.id,
                        "comentario": mensaje,
                        "user_id": ticket.user_id,
                        "es_admin": False,
                        "fecha": now.isoformat(),
                        "nombre_autor": ticket.nombre_vecino or "Vecino",
                        "origen": "public_tracking",
                        "requires_response": True,
                        "unread_for_team": True,
                    },
                }
            )
        emit_ticket_unread_changed(payload["unread_event"])
    except Exception as e:
        current_app.logger.error(f"Error emitting public tracking claim message: {e}")

    return payload


@tracking_ui_bp.route('/tracking/api/experience', methods=['GET'])
@tracking_ui_bp.route('/api/public/tracking/experience', methods=['GET'])
@limiter.limit(
    _tracking_subject_failure_rate_limit,
    key_func=_tracking_failure_subject_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
@limiter.limit(
    _tracking_failure_rate_limit,
    key_func=_tracking_failure_rate_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
def tracking_experience():
    kind = (request.args.get("kind") or request.args.get("type") or "").strip().lower()
    code = (
        request.args.get("code")
        or request.args.get("nro_ticket")
        or request.args.get("nro_pedido")
        or request.args.get("order_id")
        or ""
    ).strip()

    if kind not in {"claim", "order", "reclamo", "pedido"}:
        return _tracking_error("kind debe ser claim/order.", 400, "invalid_tracking_kind", "send_kind_claim_or_order")
    if not code:
        return _tracking_error("code requerido.", 400, "tracking_code_required", "send_tracking_code")

    if kind in {"claim", "reclamo"}:
        pin = _tracking_pin()
        if not pin:
            return _tracking_error("pin requerido.", 400, "tracking_pin_required", "send_pin")
        ticket = _find_claim_by_public_code(code, pin)
        if not ticket:
            return _tracking_error("Reclamo no encontrado.", 404, "claim_not_found", "check_code_and_pin")
        tenant = _resolve_claim_tenant(ticket)
        return _tracking_json(build_claim_tracking_experience(ticket, tenant))

    order = resolve_order_by_code(code)
    if not order:
        return _tracking_error("Pedido no encontrado.", 404, "order_not_found", "check_order_code")
    token = _tracking_access_token()
    access_granted, access_reason = validate_order_tracking_access(order, token)
    if not access_granted:
        return _tracking_error(
            "Pedido no encontrado.",
            404,
            "order_not_found",
            "check_order_code",
        )
    tenant = resolve_tenant_for_order(order)
    return _tracking_json(build_order_tracking_experience(order, tenant))


@tracking_ui_bp.route('/api/public/tracking/claims/<int:ticket_id>/messages', methods=['POST'])
@limiter.limit(
    _tracking_subject_failure_rate_limit,
    key_func=_tracking_failure_subject_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
@limiter.limit(
    _tracking_failure_rate_limit,
    key_func=_tracking_failure_rate_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
def send_public_claim_tracking_message(ticket_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}

    mensaje = (
        data.get("mensaje")
        or data.get("comentario")
        or data.get("texto")
        or ""
    )
    mensaje = str(mensaje).strip()
    pin = _tracking_pin(data)

    if not mensaje:
        return _tracking_error(
            "mensaje requerido.",
            400,
            "tracking_message_required",
            "send_non_empty_message",
        )
    if len(mensaje) > 2000:
        return _tracking_error(
            "mensaje demasiado largo.",
            400,
            "tracking_message_too_long",
            "send_shorter_message",
        )

    ticket = db.session.get(MunicipioTicket, ticket_id)
    if not ticket:
        return _tracking_error(
            "Reclamo no encontrado.",
            404,
            "claim_not_found",
            "check_code_and_pin",
        )
    if not pin:
        return _tracking_error(
            "pin requerido.",
            400,
            "tracking_pin_required",
            "send_pin",
        )
    if not getattr(ticket, "consulta_pin", None) or str(ticket.consulta_pin) != str(pin):
        return _tracking_error(
            "PIN invalido.",
            403,
            "tracking_pin_invalid",
            "send_valid_pin",
        )

    return _tracking_json(_persist_public_claim_tracking_message(ticket, mensaje), 201)

@tracking_ui_bp.route('/tracking/order/<nro_pedido>')
def tracking_page(nro_pedido):
    # 1. Fetch Order
    pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido).first()
    if not pedido:
        abort(404, "Pedido no encontrado")

    # 2. Fetch Tenant
    tenant = None
    if pedido.tenant_id:
        tenant = TenantProfile.query.get(pedido.tenant_id)

    if not tenant and pedido.pyme_id:
        # Fallback resolve via pyme_id
        tenant = TenantProfile.query.filter_by(pyme_id=pedido.pyme_id).first()
        if tenant and not pedido.tenant_id:
            # Self-healing: Link tenant_id if found
            pedido.tenant_id = tenant.id
            db.session.commit()

    if not tenant:
        abort(404, "Tienda no encontrada")

    # 3. Parse details
    try:
        detalles = json.loads(pedido.detalles or "[]")
    except:
        detalles = []

    # 4. Widget Token for Chat
    widget_token = None
    if tenant.configuracion and 'widget_tokens' in tenant.configuracion:
        tokens = tenant.configuracion['widget_tokens']
        if tokens:
            widget_token = tokens[0]

    # Fallback to legacy token resolution
    if not widget_token and tenant.pyme_id:
        owner = User.query.get(tenant.pyme_id)
        if owner and owner.entity_token:
            widget_token = owner.entity_token

    # 5. Fetch Chat History (if linked ticket exists)
    chat_history = []
    # Try to find a ticket linked to this order explicitly or via subject pattern
    linked_ticket = PymeTicket.query.filter(
        PymeTicket.tenant_id == tenant.id,
        PymeTicket.asunto.ilike(f"%{nro_pedido}%")
    ).order_by(PymeTicket.id.desc()).first()

    if linked_ticket:
        comments = linked_ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
        for c in comments:
            chat_history.append(c.to_dict())

    return render_template(
        'tracking/order_status.html',
        pedido=pedido,
        tenant=tenant,
        detalles=detalles,
        current_year=datetime.now().year,
        widget_token=widget_token,
        google_maps_key=current_app.config.get('GOOGLE_MAPS_API_KEY', ''),
        chat_history=chat_history,
    )

@tracking_ui_bp.route('/tracking/claim/<nro_ticket>')
@limiter.limit(
    _tracking_subject_failure_rate_limit,
    key_func=_tracking_failure_subject_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
@limiter.limit(
    _tracking_failure_rate_limit,
    key_func=_tracking_failure_rate_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
def tracking_claim(nro_ticket):
    # 1. Fetch Ticket
    ticket = _find_claim_by_public_code(nro_ticket)
    if not ticket:
        abort(404, "Reclamo no encontrado")
    pin = (request.args.get('pin') or '').strip()
    if getattr(ticket, 'consulta_pin', None) and str(ticket.consulta_pin) != str(pin):
        abort(403, "PIN requerido para consultar este reclamo")

    # 2. Fetch Tenant
    tenant = None
    if ticket.tenant_id:
        tenant = TenantProfile.query.get(ticket.tenant_id)

    if not tenant and ticket.municipio_id:
        tenant = TenantProfile.query.filter_by(municipio_id=ticket.municipio_id).first()
        if tenant and not ticket.tenant_id:
            ticket.tenant_id = tenant.id
            db.session.commit()

    if not tenant:
        abort(404, "Municipio no encontrado")

    # 3. Widget Token (Optional, for context)
    widget_token = None
    if tenant.configuracion and 'widget_tokens' in tenant.configuracion:
        tokens = tenant.configuracion['widget_tokens']
        if tokens:
            widget_token = tokens[0]

    # 4. Chat History
    chat_history = []
    comments = ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
    for c in comments:
        chat_history.append(c.to_dict())

    return render_template(
        'tracking/claim_status.html',
        ticket=ticket,
        tenant=tenant,
        current_year=datetime.now().year,
        widget_token=widget_token,
        google_maps_key=current_app.config.get('GOOGLE_MAPS_API_KEY', ''),
        chat_history=chat_history,
        pin=pin,
    )

@tracking_ui_bp.route('/tracking/api/send-message', methods=['POST'])
def send_message():
    data = request.json or {}
    nro_pedido = data.get('nro_pedido')
    mensaje = data.get('mensaje')

    if not nro_pedido or not mensaje:
        return jsonify({'error': 'Faltan datos'}), 400

    pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido).first()
    if not pedido:
        return jsonify({'error': 'Pedido no encontrado'}), 404

    tenant = None
    if pedido.tenant_id:
        tenant = TenantProfile.query.get(pedido.tenant_id)
    if not tenant: # Fallback
         tenant = TenantProfile.query.filter_by(pyme_id=pedido.pyme_id).first()

    if not tenant:
        return jsonify({'error': 'Tenant no encontrado'}), 404

    # 1. Update Order model (buyer_notes) if exists (Backup/Legacy)
    try:
        order_model = Order.query.filter_by(id=nro_pedido).first()
        if not order_model and pedido.tenant_id:
            order_model = servicio_pedidos.sync_order_model_from_pyme(pedido)

        if order_model:
            timestamp = datetime.now().strftime("%d/%m %H:%M")
            new_note = f"[{timestamp}] Cliente: {mensaje}"
            if order_model.buyer_notes:
                order_model.buyer_notes += f"\n{new_note}"
            else:
                order_model.buyer_notes = new_note
            db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error syncing order notes: {e}")

    # 2. Create/Update Ticket for Real-time Chat
    # Check if a ticket already exists for this order context
    ticket = PymeTicket.query.filter(
        PymeTicket.tenant_id == tenant.id,
        PymeTicket.asunto.ilike(f"%{nro_pedido}%")
    ).order_by(PymeTicket.id.desc()).first()

    comment = None
    user_id = pedido.user_id

    if not ticket:
        # Create new Ticket
        # Manually create PymeTicket
        nro_ticket = random.randint(100000, 999999)

        ticket = PymeTicket(
            tenant_id=tenant.id,
            pregunta=mensaje, # Initial question
            asunto=f"Consulta sobre Pedido {nro_pedido}",
            categoria="Pedidos",
            estado="nuevo",
            nro_ticket=nro_ticket,
            user_id=user_id,
            email=pedido.email_cliente,
            telefono=pedido.telefono_cliente,
            fecha=datetime.now()
        )
        db.session.add(ticket)
        db.session.commit()
    else:
        # Add comment to existing ticket
        comment = TicketComentario(
            pyme_ticket_id=ticket.id,
            comentario=mensaje,
            fecha=datetime.now(),
            es_admin=False, # From customer
            origen="tracking_page",
            user_id=pedido.user_id
        )
        db.session.add(comment)

        if ticket.estado in ['resuelto', 'cerrado']:
            ticket.estado = 'abierto' # Re-open if customer replies

        db.session.commit()

    # 3. Notify Admin (WebSocket)
    try:
        full_payload = {
            "tenant_type": "pyme",
            "tenant_id": tenant.id,
            "ticket_id": ticket.id,
            "message": {
                "comentario": mensaje,
                "user_id": user_id if ticket else pedido.user_id,
                "es_admin": False,
                "fecha": datetime.now().isoformat(),
                "nombre_autor": pedido.nombre_cliente or "Cliente"
            }
        }

        emit_new_chat_message(full_payload)

    except Exception as e:
        current_app.logger.error(f"Error emitting socket event: {e}")

    return jsonify({
        'status': 'ok',
        'message': 'Mensaje enviado',
        'ticket_id': ticket.id,
        'chat_entry': {
            "comentario": mensaje,
            "fecha": datetime.now().isoformat(),
            "es_admin": False,
            "autor": "vecino", # UI uses this class
            "autor_nombre": pedido.nombre_cliente or "Yo"
        }
    })

@tracking_ui_bp.route('/tracking/api/send-claim-message', methods=['POST'])
@limiter.limit(
    _tracking_subject_failure_rate_limit,
    key_func=_tracking_failure_subject_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
@limiter.limit(
    _tracking_failure_rate_limit,
    key_func=_tracking_failure_rate_key,
    deduct_when=_deduct_tracking_failure,
    on_breach=_tracking_failure_rate_limited,
)
def send_claim_message():
    data = request.json or {}
    raw_ticket = str(data.get('nro_ticket') or '').strip()
    mensaje = str(data.get('mensaje') or '').strip()
    pin = _tracking_pin(data)

    if not raw_ticket or not mensaje:
        return jsonify({'error': 'Faltan datos'}), 400
    if len(mensaje) > 2000:
        return jsonify({'error': 'Mensaje demasiado largo'}), 400

    ticket = _find_claim_by_public_code(raw_ticket)
    if not ticket:
        return jsonify({'error': 'Ticket no encontrado'}), 404
    if getattr(ticket, 'consulta_pin', None) and str(ticket.consulta_pin) != str(pin):
        return jsonify({'error': 'PIN invalido'}), 403

    payload = _persist_public_claim_tracking_message(ticket, mensaje)
    payload["legacy_contract_version"] = "tracking.claim_message.v1"
    payload["legacy_endpoint"] = "/tracking/api/send-claim-message"
    return _tracking_json(payload)
