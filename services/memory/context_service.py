from models import db
from models_memory import Contact, ContactSnapshot
from models import Order, MunicipioTicket

class ContextService:
    @staticmethod
    def build_context_for_llm(tenant_id, contact_id):
        """
        Retrieves a compressed context string for the LLM.
        """
        contact = Contact.query.get(contact_id)
        if not contact:
            return ""

        snapshot = ContactSnapshot.query.filter_by(contact_id=contact_id).first()

        # Last Orders
        last_orders = Order.query.filter_by(tenant_id=tenant_id)\
            .filter(Order.buyer_phone == contact.phone)\
            .order_by(Order.created_at.desc()).limit(3).all()

        orders_summary = "\n".join([f"- Pedido {o.id[:8]}: {o.total} {o.currency} ({o.status})" for o in last_orders])

        # Open Tickets
        # Assuming linking via phone logic for now since ticket schema varies
        # (Enhancement: Link Ticket to Contact ID in future refactor)

        context_str = f"""
        Usuario: {contact.name or 'Desconocido'}
        Perfil: {contact.type}
        Resumen: {snapshot.summary_text if snapshot else 'Sin historial previo.'}
        Últimas compras:
        {orders_summary}
        """
        return context_str
