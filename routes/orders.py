from flask import Blueprint, abort, request, jsonify, g
from models import db, Order, OrderItem, CatalogoItem, User
from middleware.tenant_context import require_tenant
from services.notification_dispatcher import notification_dispatcher
from utils.auth_helpers import token_requerido
from utils.auth_decorators import _is_authorized_for_tenant
from utils.permissions import require_role
from datetime import datetime

orders_bp = Blueprint('orders_bp', __name__)


def _ensure_tenant_operator(current_user, tenant):
    if not tenant or not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")


def _tenant_config(tenant) -> dict:
    return tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}


@orders_bp.route('/api/orders', methods=['POST'])
@require_tenant
def create_order():
    """
    Public/Widget endpoint to create a new order.
    """
    tenant = g.tenant_profile
    data = request.json or {}

    # 1. Validate Basic Data
    items_data = data.get('items', [])
    if not items_data:
        return jsonify({"error": "No items provided"}), 400

    buyer_data = data.get('buyer', {})
    delivery_data = data.get('delivery', {})

    # 2. Calculate Totals & Verify Items
    total = 0
    order_items = []

    for item in items_data:
        # Check catalog item if ID provided
        catalog_item = None
        c_id = item.get('catalog_item_id') or item.get('id')
        if c_id:
            catalog_item = CatalogoItem.query.filter_by(id=c_id, tenant_id=tenant.id).first()

        # SECURITY: Enforce server-side price if catalog item exists
        if catalog_item:
            # Use database price, ignore client price for catalog items
            unit_price = float(catalog_item.precio_monetario or 0)
        else:
            cfg = _tenant_config(tenant)
            if not cfg.get("allow_public_ad_hoc_orders", True):
                return jsonify({"error": "Catalog item is required for this tenant"}), 400
            # Public/manual items are accepted as quote requests, but the buyer
            # cannot set the final price from the client payload.
            unit_price = 0.0

        qty = int(item.get('quantity') or 1)
        if qty < 1: continue

        line_total = unit_price * qty
        total += line_total

        order_item = OrderItem(
            catalog_item_id=catalog_item.id if catalog_item else None,
            sku=catalog_item.sku if catalog_item else item.get('sku'),
            title=item.get('title') or (catalog_item.nombre if catalog_item else "Unknown Item"),
            quantity=qty,
            unit_price=unit_price,
            total_price=line_total,
            meta_data=item.get('meta')
        )
        order_items.append(order_item)

    if not order_items:
        return jsonify({"error": "No valid items"}), 400

    # 3. Create Order
    # Attempt to resolve customer from token or phone
    customer_id = None
    if g.get('viewer') and isinstance(g.viewer, User):
        customer_id = g.viewer.id

    # TODO: Create User if anon but has phone? For now, store in buyer fields.

    new_order = Order(
        tenant_id=tenant.id,
        customer_id=customer_id,
        buyer_name=buyer_data.get('name'),
        buyer_email=buyer_data.get('email'),
        buyer_phone=buyer_data.get('phone'),
        buyer_notes=data.get('notes'),
        channel=data.get('channel', 'web_widget'),
        status='created', # Initial status
        subtotal=total,
        total=total, # Add shipping/discount logic here if needed
        delivery_address=delivery_data
    )

    try:
        db.session.add(new_order)
        # Flush to get ID if needed, but UUID is generated python-side usually or DB side.
        # Here default is python lambda, so it should be available.

        for oi in order_items:
            new_order.items.append(oi)

        db.session.commit()

        # 4. Dispatch Notifications
        # We updated NotificationDispatcher to support the Order model polymorphically.
        try:
            notification_dispatcher.dispatch_order_created(new_order)
        except Exception as e:
            # Log but don't fail the request
            from flask import current_app
            current_app.logger.error(f"Notification dispatch failed for order {new_order.id}: {e}")

        return jsonify({
            "success": True,
            "order_id": new_order.id,
            "status": new_order.status,
            "total": new_order.total
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@orders_bp.route('/api/admin/orders', methods=['GET'])
@orders_bp.route('/api/orders', methods=['GET'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def list_admin_orders(current_user):
    """
    Admin endpoint to list orders.
    """
    tenant = g.tenant_profile
    _ensure_tenant_operator(current_user, tenant)

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    status = request.args.get('status')

    # Unified Query Logic (Order + PymePedido)
    from models import PymePedido

    # 1. New Orders
    query_new = Order.query.filter_by(tenant_id=tenant.id)
    if status:
        query_new = query_new.filter_by(status=status)
    new_orders = query_new.order_by(Order.created_at.desc()).limit(per_page * page).all()

    # 2. Legacy Orders
    # Handle optional pyme_id fallback
    pyme_id_filter = tenant.pyme_id if tenant.pyme_id else -1
    query_legacy = PymePedido.query.filter(
        (PymePedido.tenant_id == tenant.id) | (PymePedido.pyme_id == pyme_id_filter)
    )
    if status:
        query_legacy = query_legacy.filter(PymePedido.estado == status)
    legacy_orders = query_legacy.order_by(PymePedido.fecha.desc()).limit(per_page * page).all()

    # 3. Merge & Sort
    combined = []
    for o in new_orders:
        d = o.to_dict()
        d['_sort_date'] = o.created_at
        d['source_type'] = 'new'
        # Ensure total is at root for table consistency
        if 'total' not in d and 'totals' in d and 'total' in d['totals']:
            d['total'] = d['totals']['total']
        combined.append(d)

    for o in legacy_orders:
        d = o.to_dict()
        d['_sort_date'] = o.fecha
        d['source_type'] = 'legacy'
        # Map legacy fields to match new frontend expectations if needed
        if 'total' not in d and 'monto_total' in d:
             d['total'] = d['monto_total']
        if 'status' not in d and 'estado' in d:
             d['status'] = d['estado']
        combined.append(d)

    # Sort descending
    combined.sort(key=lambda x: x['_sort_date'] or datetime.min, reverse=True)

    # 4. Manual Pagination Slice
    start = (page - 1) * per_page
    end = start + per_page
    sliced_items = combined[start:end]

    # Rough total count estimate (sum of counts would be better but expensive)
    total_count = len(combined)
    import math
    total_pages = math.ceil(total_count / per_page) if per_page else 1

    return jsonify({
        "items": sliced_items,
        "total": total_count,
        "pages": total_pages,
        "current_page": page
    })

@orders_bp.route('/api/admin/orders/<order_id>', methods=['PATCH'])
@token_requerido
@require_role("admin", "empleado", "super_admin")
@require_tenant
def update_order(current_user, order_id):
    tenant = g.tenant_profile
    _ensure_tenant_operator(current_user, tenant)
    order = Order.query.filter_by(id=order_id, tenant_id=tenant.id).first()
    if not order:
        return jsonify({"error": "Order not found"}), 404

    data = request.json or {}

    if 'status' in data:
        # TODO: Validate status transition
        order.status = data['status']
        # Dispatch update notification?

    if 'buyer_notes' in data:
        order.buyer_notes = data['buyer_notes']

    db.session.commit()
    return jsonify(order.to_dict())
