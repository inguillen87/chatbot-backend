from models import IntegrationAccount, CatalogoItem, db
import logging

logger = logging.getLogger(__name__)

class TiendaNubeService:
    def __init__(self, tenant):
        self.tenant = tenant
        self.integration = IntegrationAccount.query.filter_by(
            tenant_id=tenant.id, type='TiendaNube'
        ).first()

    def is_connected(self):
        return self.integration is not None and self.integration.status == 'active'

    def import_products(self):
        """
        Import products from TiendaNube.
        """
        if not self.is_connected():
            return {"error": "Integration not connected", "success": False}

        try:
            # Placeholder for API call to TiendaNube
            # products = tn_api.get_products(self.integration.credentials['access_token'])
            products = [] # Mock

            count = 0
            for p in products:
                # Update or create CatalogoItem
                count += 1

            return {"success": True, "imported_count": count}
        except Exception as e:
            logger.error(f"Error importing TiendaNube products for tenant {self.tenant.slug}: {e}")
            return {"success": False, "error": str(e)}
