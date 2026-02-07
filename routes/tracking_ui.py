from flask import Blueprint, render_template, request, jsonify, abort, current_app
from models import PymePedido, TenantProfile, db, Order, User, PymeTicket, TicketComentario, MunicipioTicket
from services.pedido_service import servicio_pedidos
import json
from datetime import datetime
from services.ticket_service import servicio_tickets
from socket_service import emit_new_chat_message
import random

tracking_ui_bp = Blueprint('tracking_ui_bp', __name__)

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

    if not nro_ticket or not mensaje:
        return jsonify({'error': 'Faltan datos'}), 400

    ticket = MunicipioTicket.query.filter_by(nro_ticket=nro_ticket).first()
    if not ticket:
        return jsonify({'error': 'Ticket no encontrado'}), 404

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
