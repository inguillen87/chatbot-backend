from flask import Blueprint, jsonify, g
from utils.auth_helpers import token_requerido
from middleware.tenant_context import require_tenant
from models_memory import Contact, ContactSnapshot, InteractionEvent
from models import Order, MunicipioTicket

crm_bp = Blueprint('crm_bp', __name__)

@crm_bp.route('/api/admin/tenants/<slug>/contacts', methods=['GET'])
@token_requerido
@require_tenant
def list_contacts(current_user, slug):
    tenant = g.tenant_profile
    contacts = Contact.query.filter_by(tenant_id=tenant.id).limit(50).all()

    return jsonify({
        "contacts": [{
            "id": c.id,
            "name": c.name or "Unknown",
            "phone": c.phone,
            "type": c.type,
            "total_orders": c.total_orders,
            "ltv": float(c.ltv_monetary or 0),
            "last_interaction": c.last_interaction_at.isoformat() if c.last_interaction_at else None
        } for c in contacts]
    })

@crm_bp.route('/api/admin/tenants/<slug>/contacts/<contact_id>/history', methods=['GET'])
@token_requerido
@require_tenant
def get_contact_history(current_user, slug, contact_id):
    tenant = g.tenant_profile
    contact = Contact.query.filter_by(id=contact_id, tenant_id=tenant.id).first_or_404()
    snapshot = ContactSnapshot.query.filter_by(contact_id=contact.id).first()

    # Fetch recent orders
    orders = Order.query.filter_by(tenant_id=tenant.id)\
        .filter(Order.buyer_phone == contact.phone)\
        .order_by(Order.created_at.desc()).limit(10).all()

    # Fetch recent interactions
    interactions = InteractionEvent.query.filter_by(contact_id=contact.id)\
        .order_by(InteractionEvent.created_at.desc()).limit(20).all()

    return jsonify({
        "contact": {
            "id": contact.id,
            "name": contact.name,
            "phone": contact.phone,
            "tags": contact.tags,
            "preferences": contact.preferences
        },
        "snapshot": {
            "summary": snapshot.summary_text if snapshot else None,
            "last_intent": snapshot.last_intent if snapshot else None,
            "suggested_actions": snapshot.suggested_actions if snapshot else []
        },
        "orders": [o.to_dict() for o in orders],
        "interactions": [{
            "channel": i.channel,
            "direction": i.direction,
            "content": i.content,
            "ts": i.created_at.isoformat()
        } for i in interactions]
    })
