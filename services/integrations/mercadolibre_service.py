"""Mercado Libre Integration Service."""
from models import MarketOrder, db


def _raise_legacy_transport_disabled():
    raise RuntimeError("legacy_integration_transport_disabled")


class MercadoLibreService:
    @staticmethod
    def get_auth_url(_tenant_id, _redirect_uri):
        """Do not create OAuth URLs with request-derived tenant state."""

        _raise_legacy_transport_disabled()

    @staticmethod
    def handle_callback(_tenant_id, _code, _redirect_uri):
        """Do not exchange a code selected by an unsigned/replayable state."""

        _raise_legacy_transport_disabled()

    @staticmethod
    def process_webhook(_payload, _tenant_id=None):
        """Do not process an event without a verified tenant-bound adapter."""

        _raise_legacy_transport_disabled()

    @staticmethod
    def _map_order(tenant_id, data):
        external_id = str(data.get("id"))

        order = MarketOrder.legacy_safe_query().filter_by(
            tenant_id=tenant_id,
            external_provider="mercadolibre",
            external_order_id=external_id
        ).first()

        if not order:
            order = MarketOrder(
                tenant_id=tenant_id,
                external_provider="mercadolibre",
                external_order_id=external_id,
                channel="mercadolibre"
            )

        # Map fields
        order.status = "paid" if data.get("status") == "paid" else "pending"
        order.total_monetary = data.get("total_amount")
        order.currency = data.get("currency_id")

        buyer = data.get("buyer", {})
        order.contact_name = f"{buyer.get('first_name','')} {buyer.get('last_name','')}".strip()

        # Items
        db.session.add(order)
        db.session.flush() # Get ID

        # Sync items (simple replace or update)
        # For MVP, we just ensure order exists.

        from services.notification_dispatcher import dispatch_order_update
        dispatch_order_update(order, f"Nuevo pedido ML #{external_id}")

        db.session.commit()
