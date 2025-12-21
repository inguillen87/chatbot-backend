"""Mercado Libre Integration Service."""
import requests
import json
from datetime import datetime, timedelta, timezone
from models import db, IntegrationAccount, MarketOrder, MarketOrderItem, IntegrationEvent
from flask import current_app

ML_AUTH_URL = "https://auth.mercadolibre.com.ar/authorization"
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_API_URL = "https://api.mercadolibre.com"

class MercadoLibreService:
    @staticmethod
    def get_auth_url(tenant_id, redirect_uri):
        app_id = current_app.config.get("ML_APP_ID")
        return f"{ML_AUTH_URL}?response_type=code&client_id={app_id}&redirect_uri={redirect_uri}&state={tenant_id}"

    @staticmethod
    def handle_callback(tenant_id, code, redirect_uri):
        app_id = current_app.config.get("ML_APP_ID")
        client_secret = current_app.config.get("ML_CLIENT_SECRET")

        payload = {
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri
        }

        resp = requests.post(ML_TOKEN_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

        # Save/Update IntegrationAccount
        account = IntegrationAccount.query.filter_by(tenant_id=tenant_id, type="mercadolibre").first()
        if not account:
            account = IntegrationAccount(tenant_id=tenant_id, type="mercadolibre", credentials={})

        account.credentials = data
        account.last_sync_at = datetime.now(timezone.utc)
        account.status = "active"
        db.session.add(account)
        db.session.commit()
        return account

    @staticmethod
    def process_webhook(payload, tenant_id=None):
        """Idempotent webhook processing."""
        # ML Webhooks usually send only resource ID. We need to fetch it.
        # Example: {"resource": "/orders/123", "topic": "orders"}

        topic = payload.get("topic")
        resource = payload.get("resource")

        if topic != "orders":
            return {"status": "ignored", "reason": "not_an_order"}

        event_id = resource.split("/")[-1] if resource else "unknown"

        # Check Dedupe
        # Note: If tenant_id is unknown (ML global webhook), we might need to find it by user_id in the fetched order.
        # For now, assuming tenant_id is resolved via URL param or similar mechanism if possible.
        # If tenant_id is NOT passed, we fetch the order using ALL active accounts. (Expensive but standard for multi-tenant ML apps).

        if not tenant_id:
            # TODO: Implement lookup by scanning integration accounts or using a 'notification_secret'
            return {"status": "error", "reason": "tenant_id_required"}

        existing = IntegrationEvent.query.filter_by(
            tenant_id=tenant_id, provider="mercadolibre", event_id=event_id
        ).first()

        if existing and existing.processed:
            return {"status": "ok", "reason": "already_processed"}

        if not existing:
            existing = IntegrationEvent(
                tenant_id=tenant_id,
                provider="mercadolibre",
                event_id=event_id,
                event_type=topic,
                payload=payload
            )
            db.session.add(existing)
            db.session.commit()

        # Fetch Order Details
        account = IntegrationAccount.query.filter_by(tenant_id=tenant_id, type="mercadolibre").first()
        if not account:
             existing.error = "No integration account found"
             db.session.commit()
             return {"status": "error"}

        token = account.credentials.get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        try:
            r = requests.get(f"{ML_API_URL}{resource}", headers=headers)
            if r.status_code == 401:
                # Refresh token logic here
                pass
            r.raise_for_status()
            order_data = r.json()

            # Map to MarketOrder
            MercadoLibreService._map_order(tenant_id, order_data)

            existing.processed = True
            db.session.commit()
            return {"status": "ok"}

        except Exception as e:
            existing.error = str(e)
            db.session.commit()
            raise e

    @staticmethod
    def _map_order(tenant_id, data):
        external_id = str(data.get("id"))

        order = MarketOrder.query.filter_by(
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
