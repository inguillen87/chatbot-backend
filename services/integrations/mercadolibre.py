from models import IntegrationAccount, CatalogoItem, db
import logging
import requests

logger = logging.getLogger(__name__)

class MercadoLibreService:
    API_URL = "https://api.mercadolibre.com"

    def __init__(self, tenant):
        self.tenant = tenant
        self.integration = IntegrationAccount.query.filter_by(
            tenant_id=tenant.id, type='MercadoLibre'
        ).first()

    def is_connected(self):
        return self.integration is not None and self.integration.status == 'active'

    def _get_headers(self):
        if not self.is_connected():
            return {}
        return {"Authorization": f"Bearer {self.integration.credentials.get('access_token')}"}

    def sync_catalog(self):
        """
        Import active publications from MercadoLibre as items in the local catalog.
        """
        if not self.is_connected():
            return {"error": "Integration not connected", "success": False}

        try:
            # 1. Get User ID
            headers = self._get_headers()
            user_resp = requests.get(f"{self.API_URL}/users/me", headers=headers)
            user_resp.raise_for_status()
            user_id = user_resp.json().get("id")

            # 2. Search Active Items
            search_url = f"{self.API_URL}/users/{user_id}/items/search?status=active"
            search_resp = requests.get(search_url, headers=headers)
            search_resp.raise_for_status()
            item_ids = search_resp.json().get("results", [])

            count = 0
            # 3. Fetch Item Details (in batches ideally, simplified here)
            for item_id in item_ids:
                item_resp = requests.get(f"{self.API_URL}/items/{item_id}", headers=headers)
                if item_resp.ok:
                    item_data = item_resp.json()
                    # TODO: Create or Update CatalogoItem here
                    count += 1

            return {"success": True, "synced_count": count}
        except Exception as e:
            logger.error(f"Error syncing ML catalog for tenant {self.tenant.slug}: {e}")
            return {"success": False, "error": str(e)}

    def preview_sync(self):
        """
        Fetches items from MercadoLibre and returns a preview of how they would be mapped.
        Does NOT save changes to the database.
        """
        if not self.is_connected():
            # For demo purposes, if not connected, return a mock/error so UI can be tested
            return {"error": "Integration not connected", "success": False}

        try:
            headers = self._get_headers()

            # 1. Get User ID
            user_resp = requests.get(f"{self.API_URL}/users/me", headers=headers)
            user_resp.raise_for_status()
            user_id = user_resp.json().get("id")

            # 2. Search Items
            search_url = f"{self.API_URL}/users/{user_id}/items/search?status=active&limit=20"
            search_resp = requests.get(search_url, headers=headers)
            search_resp.raise_for_status()
            item_ids = search_resp.json().get("results", [])

            if not item_ids:
                return {"success": True, "items": [], "summary": {"total_found": 0, "new_items": 0, "updates": 0}}

            # 3. Fetch Item Details (multiget)
            ids_str = ",".join(item_ids)
            items_resp = requests.get(f"{self.API_URL}/items?ids={ids_str}", headers=headers)
            items_resp.raise_for_status()
            items_data = [i.get("body") for i in items_resp.json() if i.get("code") == 200]

            preview_data = []
            new_count = 0
            update_count = 0

            for item in items_data:
                # Basic Mapping Logic
                mapped_item = {
                    "external_id": item.get("id"),
                    "title": item.get("title"),
                    "original_price": item.get("price"),
                    "original_currency": item.get("currency_id"),
                    "image_url": item.get("thumbnail"),
                    "status": item.get("status"),
                    "permalink": item.get("permalink"),
                    "mapped_category": "General", # Would need category prediction logic
                    "mapped_price": item.get("price"),
                    "will_create_new": True
                }

                # Check if exists in local DB (by SKU or integration ID mapping)
                # existing = CatalogoItem.query...
                # if existing:
                #    mapped_item["will_create_new"] = False
                #    update_count += 1
                # else:
                new_count += 1

                preview_data.append(mapped_item)

            return {
                "success": True,
                "items": preview_data,
                "summary": {
                    "total_found": len(item_ids),
                    "new_items": new_count,
                    "updates": update_count
                }
            }

        except Exception as e:
            logger.error(f"Error previewing ML sync for tenant {self.tenant.slug}: {e}")
            return {"success": False, "error": str(e)}

    def get_unanswered_questions(self):
        if not self.is_connected():
            return []
        return []

    def reply_to_question(self, question_id, text):
        if not self.is_connected():
            return False
        return True
