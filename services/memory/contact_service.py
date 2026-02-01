from models import db
from models_memory import Contact, ContactSnapshot
import uuid
from sqlalchemy.dialects.postgresql import insert

class ContactService:
    @staticmethod
    def get_or_create_contact(tenant_id, identifier, channel='whatsapp'):
        """
        Resolves a contact by phone/email/external_id.
        """
        # Simple phone-based resolution for now
        if channel == 'whatsapp':
            phone = identifier
            contact = Contact.query.filter_by(tenant_id=tenant_id, phone=phone).first()
            if not contact:
                contact = Contact(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    phone=phone,
                    whatsapp_id=identifier,
                    type="lead"
                )
                db.session.add(contact)
                db.session.commit()
            return contact
        return None

    @staticmethod
    def update_snapshot(contact_id, summary, intent=None):
        """
        Updates the memory snapshot.
        """
        stmt = insert(ContactSnapshot).values(
            contact_id=contact_id,
            summary_text=summary,
            last_intent=intent
        ).on_conflict_do_update(
            index_elements=['contact_id'],
            set_={
                'summary_text': summary,
                'last_intent': intent,
                'updated_at': db.func.now()
            }
        )
        db.session.execute(stmt)
        db.session.commit()
