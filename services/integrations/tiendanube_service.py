"""Tienda Nube Integration Service."""
import requests
import json
from datetime import datetime, timezone
from models import db, IntegrationAccount, MarketOrder
from flask import current_app

class TiendaNubeService:
    @staticmethod
    def get_auth_url(tenant_id, redirect_uri):
        app_id = current_app.config.get("TIENDANUBE_CLIENT_ID")
        return f"https://www.tiendanube.com/apps/{app_id}/authorize?state={tenant_id}"

    @staticmethod
    def handle_callback(tenant_id, code, redirect_uri):
        # Implementation similar to ML but following TN specs
        pass
