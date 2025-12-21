from flask import Blueprint, request, jsonify, g
from models import db, MarketOrder, MarketOrderItem
from utils.auth_helpers import token_requerido
from utils.tenant import require_tenant

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
            "items_count": len(o.items)
        } for o in orders]
    })

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
