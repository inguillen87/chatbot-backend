from models import db, TimestampMixin, JSONType
from sqlalchemy import Index

class Contact(db.Model, TimestampMixin):
    """
    Represents a unified contact identity across channels within a tenant.
    """
    __tablename__ = "contact"

    id = db.Column(db.String(36), primary_key=True)  # UUID
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)

    # Core Identity
    name = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True, index=True) # Normalized E.164
    email = db.Column(db.String(255), nullable=True, index=True)

    # External IDs (for mapping)
    external_id = db.Column(db.String(255), nullable=True, index=True) # General external ID
    whatsapp_id = db.Column(db.String(50), nullable=True, index=True)

    # Classification
    type = db.Column(db.String(20), default="unknown") # neighbor, customer, lead, employee
    tags = db.Column(JSONType, default=[]) # ["vip", "recurrent", "complainant"]

    # Preferences (JSON)
    preferences = db.Column(JSONType, default={}) # {"channel": "whatsapp", "payment": "mp"}

    # Metrics
    ltv_monetary = db.Column(db.Numeric(12, 2), default=0)
    total_orders = db.Column(db.Integer, default=0)
    last_interaction_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_contact_tenant_phone", "tenant_id", "phone"),
    )

class ContactSnapshot(db.Model, TimestampMixin):
    """
    Stores a summarized 'memory' of the contact for quick retrieval by LLM.
    Updated asynchronously.
    """
    __tablename__ = "contact_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.String(36), db.ForeignKey("contact.id", ondelete="CASCADE"), nullable=False, unique=True)

    summary_text = db.Column(db.Text, nullable=True) # "Juan suele comprar vinos tintos y paga con QR."
    last_intent = db.Column(db.String(100), nullable=True)
    suggested_actions = db.Column(JSONType, default=[]) # ["offer_refill", "ask_about_ticket"]

    embedding = db.Column(JSONType, nullable=True) # Optional: Embedding of the summary

class InteractionEvent(db.Model, TimestampMixin):
    """
    Raw history of interactions (messages, calls, etc.) linked to a contact.
    """
    __tablename__ = "interaction_event"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    contact_id = db.Column(db.String(36), db.ForeignKey("contact.id"), nullable=True, index=True)

    channel = db.Column(db.String(20), nullable=False) # whatsapp, widget, voice
    direction = db.Column(db.String(10), nullable=False) # inbound, outbound

    content_type = db.Column(db.String(20), default="text") # text, audio, image, location
    content = db.Column(db.Text, nullable=True) # Text body or transcript
    media_url = db.Column(db.String(500), nullable=True)

    metadata_payload = db.Column(JSONType, default={}) # duration, status, etc.

class LoyaltyAccount(db.Model, TimestampMixin):
    __tablename__ = "loyalty_account"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False)
    contact_id = db.Column(db.String(36), db.ForeignKey("contact.id"), nullable=False)

    balance = db.Column(db.Integer, default=0)
    tier = db.Column(db.String(50), default="standard")

    __table_args__ = (
        db.UniqueConstraint("tenant_id", "contact_id", name="uq_loyalty_contact"),
    )

class PointsLedger(db.Model, TimestampMixin):
    __tablename__ = "points_ledger"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("loyalty_account.id"), nullable=False, index=True)

    amount = db.Column(db.Integer, nullable=False) # Positive or negative
    reason = db.Column(db.String(50), nullable=False) # purchase, redemption, bonus
    reference_id = db.Column(db.String(100), nullable=True) # order_id

    balance_snapshot = db.Column(db.Integer, nullable=False)
