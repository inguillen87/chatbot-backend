from flask import Blueprint, abort, request, jsonify, g
from sqlalchemy import func

from models import db, Order, OrderItem, CatalogoItem, User, MarketOrder, PedidoConversacional, PymePedido
from middleware.tenant_context import require_tenant
from services.commerce_unified import dedupe_unified_orders, serialize_unified_order
from services.notification_dispatcher import notification_dispatcher
from utils.auth_helpers import token_requerido
from utils.auth_decorators import _is_authorized_for_tenant
from utils.permissions import require_role

orders_bp = Blueprint('orders_bp', __name__)


def _ensure_tenant_operator(current_user, tenant):
    if not tenant or not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")


def _tenant_config(tenant) -> dict:
    return tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}


def _with_admin_order_legacy_aliases(order_payload: dict) -> dict:
    payload = dict(order_payload)
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    if "buyer" not in payload:
        payload["buyer"] = {
            "name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
        }
    if "source_type" not in payload:
        source_model = str(payload.get("source_model") or "").lower()
        payload["source_type"] = {
            "order": "new",
            "pymepedido": "legacy",
            "marketorder": "market",
            "pedidoconversacional": "assisted_intake",
        }.get(source_model, source_model or "unknown")
    return payload


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

    status_filter = status.strip().lower() if isinstance(status, str) and status.strip() else None
    per_source_limit = max(1, min(per_page * page, 200))
    order_records = []

    canonical_query = Order.query.filter(Order.tenant_id == tenant.id)
    if status_filter:
        canonical_query = canonical_query.filter(func.lower(Order.status) == status_filter)
    order_records.extend(canonical_query.order_by(Order.created_at.desc()).limit(per_source_limit).all())

    pyme_id_filter = tenant.pyme_id if tenant.pyme_id else -1
    legacy_query = PymePedido.query.filter(
        (PymePedido.tenant_id == tenant.id) | (PymePedido.pyme_id == pyme_id_filter)
    )
    if status_filter:
        legacy_query = legacy_query.filter(func.lower(PymePedido.estado) == status_filter)
    order_records.extend(legacy_query.order_by(PymePedido.fecha.desc()).limit(per_source_limit).all())

    market_query = MarketOrder.legacy_safe_query().filter(MarketOrder.tenant_id == tenant.id)
    if status_filter:
        market_query = market_query.filter(func.lower(MarketOrder.status) == status_filter)
    order_records.extend(market_query.order_by(MarketOrder.created_at.desc()).limit(per_source_limit).all())

    conversational_query = PedidoConversacional.query.filter(PedidoConversacional.tenant_id == tenant.id)
    if status_filter:
        conversational_query = conversational_query.filter(func.lower(PedidoConversacional.estado) == status_filter)
    order_records.extend(conversational_query.order_by(PedidoConversacional.created_at.desc()).limit(per_source_limit).all())

    combined = dedupe_unified_orders([serialize_unified_order(record) for record in order_records])
    combined.sort(key=lambda item: item.get("created_at") or item.get("updated_at") or "", reverse=True)
    combined = [_with_admin_order_legacy_aliases(item) for item in combined]

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
        "current_page": page,
        "sources": sorted({item.get("source_model") for item in sliced_items if item.get("source_model")}),
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
