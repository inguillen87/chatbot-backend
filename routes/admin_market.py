from flask import Blueprint, request, jsonify, g
from models import db, MarketOrder, MarketOrderItem, CatalogoItem
from utils.auth_helpers import token_requerido
from utils.tenant import require_tenant
from sqlalchemy.orm.attributes import flag_modified

admin_market_bp = Blueprint('admin_market', __name__)

@admin_market_bp.route('/orders', methods=['GET'])
@token_requerido
@require_tenant
def list_orders(user):
    """List orders for the current tenant."""
    # Filter by status, channel
    status = request.args.get('status')
    channel = request.args.get('channel')

    query = MarketOrder.query.filter_by(tenant_id=g.tenant_profile.id)

    if status:
        query = query.filter_by(status=status)
    if channel:
        query = query.filter_by(channel=channel)

    orders = query.order_by(MarketOrder.created_at.desc()).limit(50).all()

    return jsonify({
        "orders": [{
            "id": o.id,
            "status": o.status,
            "channel": o.channel,
            "total": float(o.total_monetary or 0),
            "contact": o.contact_name,
            "external_id": o.external_order_id,
            "items_count": len(o.items),
            "created_at": o.created_at.isoformat() if o.created_at else None
        } for o in orders]
    })

@admin_market_bp.route('/orders', methods=['POST'])
@token_requerido
@require_tenant
def create_order(user):
    """Create a manual order."""
    data = request.get_json()

    tenant_id = g.tenant_profile.id

    # Client info
    contact_name = data.get('contact_name') or "Cliente Mostrador"
    contact_phone = data.get('contact_phone')
    contact_email = data.get('contact_email')

    order = MarketOrder(
        tenant_id=tenant_id,
        user_id=user.id if not data.get('anonymous') else None,
        status='confirmed', # Manual orders usually start confirmed
        channel='manual',
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_email=contact_email,
        note=data.get('note')
    )

    # Process Items
    items_data = data.get('items', [])
    total = 0

    for item_d in items_data:
        product_id = item_d.get('product_id')
        qty = int(item_d.get('quantity', 1))
        price = float(item_d.get('price', 0))

        # If product_id provided, fetch details for snapshot if price missing
        if product_id and price == 0:
            prod = db.session.get(CatalogoItem, product_id)
            if prod:
                # Try to parse price from string if necessary, or use numeric
                try:
                    price = float(prod.precio_monetario or 0)
                except:
                    price = 0

        total += price * qty

        order_item = MarketOrderItem(
            order=order,
            product_id=product_id,
            quantity=qty,
            price_monetary=price,
            name_snapshot=item_d.get('name', 'Producto Manual')
        )
        # Assuming MarketOrder relationship manages the add, but safe to add explicit
        # db.session.add(order_item) # relationship back_populates handles this if order added

    order.total_monetary = total
    db.session.add(order)
    db.session.commit()

    return jsonify({"status": "ok", "order_id": order.id}), 201

@admin_market_bp.route('/orders/<int:order_id>', methods=['PUT'])
@token_requerido
@require_tenant
def update_order(user, order_id):
    """Update order status or notes."""
    order = MarketOrder.query.filter_by(id=order_id, tenant_id=g.tenant_profile.id).first_or_404()
    data = request.get_json()

    if 'status' in data:
        order.status = data['status']
        # Dispatch notification if configured
        from services.notification_dispatcher import dispatch_order_update
        dispatch_order_update(order, f"Estado actualizado a {order.status}")

    if 'note' in data:
        order.note = data['note']

    db.session.commit()
    return jsonify({"status": "ok"})

@admin_market_bp.route('/notifications', methods=['GET', 'PUT'])
@token_requerido
@require_tenant
def notification_settings(user):
    """Manage tenant notification settings."""
    tenant = g.tenant_profile

    if request.method == 'GET':
        config = tenant.configuracion or {}
        return jsonify({
            "owner_phone": config.get("owner_notification_phone"),
            "telegram_chat_id": config.get("owner_telegram_chat_id"),
            "notification_settings": config.get("notification_settings", {})
        })

    if request.method == 'PUT':
        data = request.get_json()
        config = tenant.configuracion or {}

        # Update fields
        if 'owner_phone' in data:
            config['owner_notification_phone'] = data['owner_phone']
        if 'telegram_chat_id' in data:
            config['owner_telegram_chat_id'] = data['telegram_chat_id']
        if 'notification_settings' in data:
            config['notification_settings'] = data['notification_settings']

        tenant.configuracion = config
        flag_modified(tenant, "configuracion")
        db.session.commit()

        return jsonify({"status": "ok"})
