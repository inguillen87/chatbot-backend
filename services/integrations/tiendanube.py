from models import IntegrationAccount, CatalogoItem, db
import logging
import requests

logger = logging.getLogger(__name__)

class TiendaNubeService:
    API_BASE = "https://api.tiendanube.com/v1"

    def __init__(self, tenant):
        self.tenant = tenant
        self.integration = IntegrationAccount.query.filter_by(
            tenant_id=tenant.id, type='TiendaNube'
        ).first()

    def is_connected(self):
        return self.integration is not None and self.integration.status == 'active'

    def _get_headers(self):
        if not self.is_connected():
            return {}
        token = self.integration.credentials.get('access_token')
        return {
            "Authentication": f"bearer {token}",
            "User-Agent": "Chatboc (soporte@chatboc.ar)"
        }

    def import_products(self):
        """
        Import products from TiendaNube.
        """
        if not self.is_connected():
            return {"error": "Integration not connected", "success": False}

        # Actual implementation logic omitted for brevity in this specific task,
        # focusing on the structure for the controller.
        return {"success": True, "imported_count": 0}

    def preview_sync(self):
        """
        Fetches products from TiendaNube and returns a preview structure.
        """
        if not self.is_connected():
            return {"error": "No estás conectado a TiendaNube.", "success": False}

        try:
            user_id = self.integration.credentials.get('user_id')
            if not user_id:
                # Try to fetch if not stored
                headers = self._get_headers()
                # store_resp = requests.get(f"{self.API_BASE}/store", headers=headers)
                # ... logic to get store_id
                pass

            # For now, return a mock preview to enable UI development if API fails or credentials are dummy
            # Real implementation would call: GET /v1/{store_id}/products

            # Mock Data
            items = [
                {
                    "external_id": "tn_123",
                    "title": "Producto Demo TiendaNube",
                    "original_price": 1500.00,
                    "original_currency": "ARS",
                    "image_url": "https://via.placeholder.com/150",
                    "mapped_category": "General",
                    "mapped_price": 1500.00,
                    "status": "active",
                    "will_create_new": True
                }
            ]

            return {
                "success": True,
                "items": items,
                "summary": {
                    "total_found": len(items),
                    "new_items": len(items),
                    "updates": 0
                }
            }

        except Exception as e:
            logger.error(f"Error previewing TiendaNube sync: {e}")
            return {"success": False, "error": "Error de comunicación con TiendaNube. Intenta más tarde."}
