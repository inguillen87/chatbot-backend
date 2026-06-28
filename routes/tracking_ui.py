from flask import Blueprint, render_template, request, jsonify, abort, current_app
from models import PymePedido, TenantProfile, db, Order, User, PymeTicket, TicketComentario, MunicipioTicket
from services.pedido_service import servicio_pedidos
import json
from datetime import datetime
from services.ticket_service import servicio_tickets
from socket_service import emit_new_chat_message
import random
import uuid
from services.tracking_experience import (
    TRACKING_EXPERIENCE_CONTRACT_VERSION,
    build_claim_tracking_experience,
    build_order_tracking_experience,
    resolve_order_by_code,
    resolve_tenant_for_order,
)

tracking_ui_bp = Blueprint('tracking_ui_bp', __name__)


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
    return response


def _tracking_error(message: str, status_code: int, reason_code: str, action_hint: str):
    return _tracking_json(
        {
            "contract_version": TRACKING_EXPERIENCE_CONTRACT_VERSION,
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
        },
        status_code,
    )


@tracking_ui_bp.route('/tracking/api/experience', methods=['GET'])
@tracking_ui_bp.route('/api/public/tracking/experience', methods=['GET'])
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
        pin = (request.args.get("pin") or "").strip()
        if not pin:
            return _tracking_error("pin requerido.", 400, "tracking_pin_required", "send_pin")
        normalized = code.upper()
        normalized = normalized[2:] if normalized.startswith(("M-", "S-")) else normalized
        ticket = MunicipioTicket.query.filter_by(nro_ticket=normalized, consulta_pin=pin).first()
        if not ticket:
            return _tracking_error("Reclamo no encontrado.", 404, "claim_not_found", "check_code_and_pin")
        tenant = TenantProfile.query.get(ticket.tenant_id) if ticket.tenant_id else None
        if not tenant and ticket.municipio_id:
            tenant = TenantProfile.query.filter_by(municipio_id=ticket.municipio_id).first()
        return _tracking_json(build_claim_tracking_experience(ticket, tenant))

    order = resolve_order_by_code(code)
    if not order:
        return _tracking_error("Pedido no encontrado.", 404, "order_not_found", "check_order_code")
    tenant = resolve_tenant_for_order(order)
    return _tracking_json(build_order_tracking_experience(order, tenant))


@tracking_ui_bp.route('/api/public/tracking/claims/<int:ticket_id>/messages', methods=['POST'])
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
    pin = (data.get("pin") or request.args.get("pin") or "").strip()

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

    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=mensaje,
        fecha=datetime.now(),
        es_admin=False,
        origen="public_tracking",
        user_id=getattr(ticket, "user_id", None),
    )
    db.session.add(comment)

    if ticket.estado in ["resuelto", "cerrado"]:
        ticket.estado = "abierto"

    db.session.commit()

    tenant = db.session.get(TenantProfile, ticket.tenant_id) if ticket.tenant_id else None
    if not tenant and ticket.municipio_id:
        tenant = TenantProfile.query.filter_by(municipio_id=ticket.municipio_id).first()

    tracking_payload = build_claim_tracking_experience(ticket, tenant)
    support = tracking_payload.get("support") or {}
    try:
        if tenant:
            emit_new_chat_message(
                {
                    "tenant_type": "municipio",
                    "tenant_id": tenant.id,
                    "ticket_id": ticket.id,
                    "message": {
                        "comentario": mensaje,
                        "user_id": ticket.user_id,
                        "es_admin": False,
                        "fecha": datetime.now().isoformat(),
                        "nombre_autor": ticket.nombre_vecino or "Vecino",
                        "origen": "public_tracking",
                    },
                }
            )
    except Exception as e:
        current_app.logger.error(f"Error emitting public tracking claim message: {e}")

    live_mode = support.get("mode") or "offline"
    comment_payload = {
        "id": comment.id,
        "message": comment.comentario,
        "comentario": comment.comentario,
        "author": "customer",
        "source": comment.origen,
        "created_at": comment.fecha.isoformat() if comment.fecha else None,
    }

    return _tracking_json(
        {
            "contract_version": "tracking.support_message.v1",
            "success": True,
            "message": (
                "Mensaje enviado al canal de atencion en vivo."
                if live_mode == "live"
                else "Mensaje recibido. Queda asociado al reclamo para que el equipo lo responda."
            ),
            "ticket_id": ticket.id,
            "ticket_number": ticket.nro_ticket,
            "comment": comment_payload,
            "chat_entry": {
                "comentario": comment.comentario,
                "fecha": comment_payload["created_at"],
                "es_admin": False,
                "autor": "vecino",
                "autor_nombre": ticket.nombre_vecino or "Yo",
                "origen": comment.origen,
            },
            "delivery": {
                "mode": live_mode,
                "channel": "ticket_bound_helpdesk",
                "realtime_available": live_mode == "live",
                "offline_queue": live_mode != "live",
                "admin_surface": "tenant_claims_inbox",
            },
            "timeline_endpoint": support.get("endpoints", {}).get("timeline"),
            "tracking": tracking_payload,
        },
        201,
    )

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
        chat_history=chat_history
    )

@tracking_ui_bp.route('/tracking/claim/<nro_ticket>')
def tracking_claim(nro_ticket):
    # 1. Fetch Ticket
    ticket = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket).first()
    if not ticket:
        abort(404, "Reclamo no encontrado")

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
        chat_history=chat_history
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
def send_claim_message():
    data = request.json or {}
    nro_ticket = data.get('nro_ticket')
    mensaje = data.get('mensaje')
    pin = (data.get('pin') or request.args.get('pin') or '').strip()

    if not nro_ticket or not mensaje:
        return jsonify({'error': 'Faltan datos'}), 400

    ticket = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket).first()
    if not ticket:
        return jsonify({'error': 'Ticket no encontrado'}), 404
    if getattr(ticket, 'consulta_pin', None) and str(ticket.consulta_pin) != str(pin):
        return jsonify({'error': 'PIN invalido'}), 403

    # Add comment
    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=mensaje,
        fecha=datetime.now(),
        es_admin=False,
        origen="tracking_page",
        user_id=ticket.user_id
    )
    db.session.add(comment)

    if ticket.estado in ['resuelto', 'cerrado']:
        ticket.estado = 'abierto'

    db.session.commit()

    # Notify Admin via Socket
    try:
        tenant = TenantProfile.query.get(ticket.tenant_id) if ticket.tenant_id else None
        if tenant:
            full_payload = {
                "tenant_type": "municipio",
                "tenant_id": tenant.id,
                "ticket_id": ticket.id,
                "message": {
                    "comentario": mensaje,
                    "user_id": ticket.user_id,
                    "es_admin": False,
                    "fecha": datetime.now().isoformat(),
                    "nombre_autor": ticket.nombre_vecino or "Vecino"
                }
            }
            emit_new_chat_message(full_payload)
    except Exception as e:
        current_app.logger.error(f"Error emitting socket event: {e}")

    return jsonify({
        'status': 'ok',
        'chat_entry': {
             "comentario": mensaje,
             "fecha": datetime.now().isoformat(),
             "es_admin": False,
             "autor": "vecino",
             "autor_nombre": ticket.nombre_vecino or "Yo"
        }
    })
